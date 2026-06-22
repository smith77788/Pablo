"""Накрутчик — просмотры, реакции, сторис.

Потоки:
  Просмотры  → канал → ID сообщений → кол-во акк. → подтверждение → очередь
  Реакции    → канал → ID сообщения  → emoji       → кол-во акк.  → очередь
  Сторис     → @цель                 → кол-во акк. → подтверждение → очередь
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BoostCb, BmCb

log = logging.getLogger(__name__)
router = Router()

_EMOJI_CHOICES = ["❤", "🔥", "👍", "💯", "🎉", "👏", "😍", "🥰", "💪", "🤩"]


class BoostViews(StatesGroup):
    channel = State()
    msg_ids = State()
    acc_count = State()


class BoostReactions(StatesGroup):
    channel = State()
    msg_id = State()
    emoji = State()
    acc_count = State()


class BoostStories(StatesGroup):
    target = State()
    acc_count = State()


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


async def _get_acc_count(pool: asyncpg.Pool, owner_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
        owner_id,
    )
    return int(row["cnt"]) if row else 0


def _back_kb(action: str = "menu") -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=BoostCb(action=action))
    return kb.as_markup()


def _cancel_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BoostCb(action="menu"))
    return kb.as_markup()


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(BoostCb.filter(F.action == "menu"))
async def cb_boost_menu(
    callback: CallbackQuery,
    callback_data: BoostCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await state.clear()
    total = await _get_acc_count(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Просмотры", callback_data=BoostCb(action="views"))
    kb.button(text="❤ Реакции", callback_data=BoostCb(action="reactions"))
    kb.button(text="📖 Сторис", callback_data=BoostCb(action="stories"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(3, 1)
    text = (
        "🚀 <b>Накрутка</b>\n\n"
        "Массовые просмотры, реакции и просмотр сторис через ваши аккаунты.\n\n"
        f"🔑 Доступно аккаунтов: <b>{total}</b>\n\n"
        "Выберите тип накрутки:"
    )
    await _edit(callback, text, kb.as_markup())


# ── Просмотры ─────────────────────────────────────────────────────────────────

@router.callback_query(BoostCb.filter(F.action == "views"))
async def cb_boost_views_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(BoostViews.channel)
    await _edit(
        callback,
        "👁 <b>Накрутка просмотров</b>\n\n"
        "Введите @username канала или ссылку t.me/...\n\n"
        "<i>Просмотры увеличиваются у каждого сообщения через каждый ваш аккаунт.</i>",
        _cancel_kb(),
    )


@router.message(BoostViews.channel)
async def msg_views_channel(message: Message, state: FSMContext) -> None:
    from services.boost_engine import parse_channel_ref
    channel = parse_channel_ref(message.text or "")
    if not channel:
        await message.answer("⚠️ Не удалось распознать канал. Введите @username или t.me/link")
        return
    await state.update_data(channel=channel)
    await state.set_state(BoostViews.msg_ids)
    await message.answer(
        f"📌 Канал: <code>{html.escape(channel)}</code>\n\n"
        "Введите ID сообщений через запятую:\n"
        "<code>123, 124, 125</code>\n\n"
        "Или диапазон: <code>120-130</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(BoostViews.msg_ids)
async def msg_views_ids(message: Message, state: FSMContext) -> None:
    from services.boost_engine import parse_msg_ids
    ids = parse_msg_ids(message.text or "")
    if not ids:
        await message.answer("⚠️ Не удалось распознать ID. Введите числа через запятую: 123, 124")
        return
    await state.update_data(msg_ids=ids)
    await state.set_state(BoostViews.acc_count)
    await message.answer(
        f"✅ ID сообщений: <b>{len(ids)} шт.</b> ({', '.join(str(i) for i in ids[:5])}{'...' if len(ids) > 5 else ''})\n\n"
        "Сколько аккаунтов использовать?\n"
        "Введите число или <code>0</code> — все доступные:",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(BoostViews.acc_count)
async def msg_views_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    data = await state.get_data()
    total = await _get_acc_count(pool, message.from_user.id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов. Добавьте аккаунты в разделе Аккаунты.")
        return
    await state.update_data(acc_count=use)
    channel = data["channel"]
    ids = data["msg_ids"]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=BoostCb(action="confirm", sub="views"))
    kb.button(text="❌ Отмена", callback_data=BoostCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        "👁 <b>Накрутка просмотров — подтверждение</b>\n\n"
        f"📌 Канал: <code>{html.escape(channel)}</code>\n"
        f"📨 Сообщений: <b>{len(ids)}</b>\n"
        f"🔑 Аккаунтов: <b>{use}</b>\n"
        f"📊 Итого действий: <b>{use * len(ids)}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Реакции ───────────────────────────────────────────────────────────────────

@router.callback_query(BoostCb.filter(F.action == "reactions"))
async def cb_boost_reactions_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(BoostReactions.channel)
    await _edit(
        callback,
        "❤ <b>Накрутка реакций</b>\n\n"
        "Введите @username канала или ссылку t.me/...",
        _cancel_kb(),
    )


@router.message(BoostReactions.channel)
async def msg_reactions_channel(message: Message, state: FSMContext) -> None:
    from services.boost_engine import parse_channel_ref
    channel = parse_channel_ref(message.text or "")
    if not channel:
        await message.answer("⚠️ Не удалось распознать канал.")
        return
    await state.update_data(channel=channel)
    await state.set_state(BoostReactions.msg_id)
    await message.answer(
        f"📌 Канал: <code>{html.escape(channel)}</code>\n\n"
        "Введите ID сообщения (одно число):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(BoostReactions.msg_id)
async def msg_reactions_msg_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ Введите одно число — ID сообщения")
        return
    await state.update_data(msg_id=int(text))
    await state.set_state(BoostReactions.emoji)
    kb = InlineKeyboardBuilder()
    for em in _EMOJI_CHOICES:
        kb.button(text=em, callback_data=f"bst_emoji:{em}")
    kb.adjust(5)
    await message.answer(
        "Выберите эмодзи реакции:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("bst_emoji:"))
async def cb_pick_emoji(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    emoji = callback.data.split(":", 1)[1]
    await state.update_data(emoji=emoji)
    await state.set_state(BoostReactions.acc_count)
    total = await _get_acc_count(pool, callback.from_user.id)
    await callback.message.edit_text(
        f"Выбрана реакция: {emoji}\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько использовать? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(BoostReactions.acc_count)
async def msg_reactions_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    data = await state.get_data()
    total = await _get_acc_count(pool, message.from_user.id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    channel = data["channel"]
    msg_id = data["msg_id"]
    emoji = data["emoji"]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=BoostCb(action="confirm", sub="reactions"))
    kb.button(text="❌ Отмена", callback_data=BoostCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        "❤ <b>Накрутка реакций — подтверждение</b>\n\n"
        f"📌 Канал: <code>{html.escape(channel)}</code>\n"
        f"📨 Сообщение: <code>{msg_id}</code>\n"
        f"Реакция: {emoji}\n"
        f"🔑 Аккаунтов: <b>{use}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Сторис ────────────────────────────────────────────────────────────────────

@router.callback_query(BoostCb.filter(F.action == "stories"))
async def cb_boost_stories_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(BoostStories.target)
    await _edit(
        callback,
        "📖 <b>Просмотр сторис</b>\n\n"
        "Введите @username пользователя или канала,\n"
        "чьи сторис нужно просмотреть всеми аккаунтами:",
        _cancel_kb(),
    )


@router.message(BoostStories.target)
async def msg_stories_target(message: Message, state: FSMContext) -> None:
    from services.boost_engine import parse_channel_ref
    target = parse_channel_ref(message.text or "")
    if not target:
        await message.answer("⚠️ Не удалось распознать цель. Введите @username")
        return
    await state.update_data(target=target)
    await state.set_state(BoostStories.acc_count)
    await message.answer(
        f"📌 Цель: <code>{html.escape(target)}</code>\n\n"
        "Сколько аккаунтов использовать? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(BoostStories.acc_count)
async def msg_stories_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    data = await state.get_data()
    total = await _get_acc_count(pool, message.from_user.id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    target = data["target"]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=BoostCb(action="confirm", sub="stories"))
    kb.button(text="❌ Отмена", callback_data=BoostCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        "📖 <b>Просмотр сторис — подтверждение</b>\n\n"
        f"📌 Цель: <code>{html.escape(target)}</code>\n"
        f"🔑 Аккаунтов: <b>{use}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Подтверждение и постановка в очередь ─────────────────────────────────────

@router.callback_query(BoostCb.filter(F.action == "confirm"))
async def cb_boost_confirm(
    callback: CallbackQuery,
    callback_data: BoostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sub = callback_data.sub
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    acc_count = data.get("acc_count", 0)

    # Выбрать acc_count аккаунтов
    rows = await pool.fetch(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
        owner_id, acc_count,
    )
    account_ids = [r["id"] for r in rows]

    if not account_ids:
        await callback.answer("⚠️ Нет доступных аккаунтов", show_alert=True)
        return

    if sub == "views":
        op_type = "boost_views"
        params = {
            "channel": data["channel"],
            "msg_ids": data["msg_ids"],
            "account_ids": account_ids,
        }
        total_items = len(account_ids) * len(data["msg_ids"])
        label = f"Просмотры: {data['channel']} × {len(data['msg_ids'])} сообщений × {len(account_ids)} акк."
    elif sub == "reactions":
        op_type = "boost_reactions"
        params = {
            "channel": data["channel"],
            "msg_id": data["msg_id"],
            "emoji": data["emoji"],
            "account_ids": account_ids,
        }
        total_items = len(account_ids)
        label = f"Реакции {data['emoji']}: {data['channel']} × {len(account_ids)} акк."
    else:  # stories
        op_type = "boost_stories"
        params = {
            "target": data["target"],
            "account_ids": account_ids,
        }
        total_items = len(account_ids)
        label = f"Сторис: {data['target']} × {len(account_ids)} акк."

    import json
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,$2,'pending',$3,$4,$5) RETURNING id",
        owner_id, op_type, json.dumps(params), total_items, label,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=f"bm:op_detail:{op_id}")
    kb.button(text="◀️ В меню", callback_data=BoostCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Поставлено в очередь</b>\n\n"
        f"🆔 Операция: <b>#{op_id}</b>\n"
        f"📊 {html.escape(label)}\n\n"
        "Выполнение начнётся автоматически.",
        kb.as_markup(),
    )
