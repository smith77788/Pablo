"""Инвайтер — массовое добавление пользователей в группу.

Источники:
  1. Из парсера аудитории (база parsed_audiences)
  2. Вручную @username / ID (через запятую)
  3. По номерам телефонов
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

from bot.callbacks import InviterCb, BmCb

log = logging.getLogger(__name__)
router = Router()


class InviterFSM(StatesGroup):
    group = State()        # куда добавляем
    source = State()       # откуда берём (ждём callback выбора)
    users_manual = State() # @username/ID вручную
    phones = State()       # телефоны
    acc_count = State()    # кол-во аккаунтов


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    return kb.as_markup()


async def _acc_count(pool: asyncpg.Pool, owner_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
        owner_id,
    )
    return int(row["cnt"]) if row else 0


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "menu"))
async def cb_inviter_menu(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await state.clear()
    total_accs = await _acc_count(pool, callback.from_user.id)
    total_parsed = await pool.fetchval(
        "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1",
        callback.from_user.id,
    ) or 0
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить пользователей", callback_data=InviterCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)
    await _edit(
        callback,
        "👥 <b>Инвайтер</b>\n\n"
        "Массовое добавление пользователей в группу через ваши аккаунты.\n\n"
        f"🔑 Аккаунтов: <b>{total_accs}</b>\n"
        f"🗃 В базе парсера: <b>{total_parsed}</b> пользователей\n\n"
        "Поддерживает:\n"
        "• @username / user_id — вручную\n"
        "• Из базы парсера аудитории\n"
        "• По номерам телефонов",
        kb.as_markup(),
    )


# ── Шаг 1: Выбор группы ──────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "start"))
async def cb_inviter_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InviterFSM.group)
    await _edit(
        callback,
        "👥 <b>Инвайтер — шаг 1/3</b>\n\n"
        "Введите @username или ссылку группы, <b>куда</b> добавляем пользователей:\n\n"
        "<i>Аккаунты должны уже быть участниками этой группы.</i>",
        _cancel_kb(),
    )


@router.message(InviterFSM.group)
async def msg_inviter_group(message: Message, state: FSMContext) -> None:
    from services.mass_inviter_engine import parse_group_ref
    group = parse_group_ref(message.text or "")
    if not group:
        await message.answer("⚠️ Не удалось распознать группу. Введите @username или t.me/...")
        return
    await state.update_data(group=group)
    await state.set_state(InviterFSM.source)
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Из базы парсера", callback_data=InviterCb(action="src_parser"))
    kb.button(text="✏️ Вручную (@username/ID)", callback_data=InviterCb(action="src_manual"))
    kb.button(text="📱 По номерам телефонов", callback_data=InviterCb(action="src_phones"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ Группа: <code>{html.escape(group)}</code>\n\n"
        "<b>Откуда брать пользователей?</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Шаг 2a: Из парсера аудитории ─────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_parser"))
async def cb_src_parser(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    owner_id = callback.from_user.id
    # Показываем последние 5 парсингов
    runs = await pool.fetch(
        "SELECT parse_run_id, source_title, COUNT(*) AS cnt "
        "FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id IS NOT NULL "
        "GROUP BY parse_run_id, source_title ORDER BY parse_run_id DESC LIMIT 8",
        owner_id,
    )
    if not runs:
        await callback.answer("⚠️ База парсера пуста. Сначала запустите парсер.", show_alert=True)
        return
    await state.update_data(source_type="parser")
    kb = InlineKeyboardBuilder()
    for r in runs:
        label = f"{r['source_title'] or 'без названия'} ({r['cnt']} чел.)"
        kb.button(text=label[:50], callback_data=InviterCb(action="pick_run", item=str(r["parse_run_id"])))
    kb.button(text="🌐 Вся база", callback_data=InviterCb(action="pick_run", item="all"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "📋 Выберите источник из парсера:", kb.as_markup())


@router.callback_query(InviterCb.filter(F.action == "pick_run"))
async def cb_pick_run(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    run_id = callback_data.item
    owner_id = callback.from_user.id
    if run_id == "all":
        count = await pool.fetchval(
            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1", owner_id
        )
        await state.update_data(parse_run_id=None, total_users=count)
    else:
        count = await pool.fetchval(
            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
            owner_id, int(run_id),
        )
        await state.update_data(parse_run_id=int(run_id), total_users=count)
    await state.set_state(InviterFSM.acc_count)
    data = await state.get_data()
    await _ask_acc_count(callback, data, count, pool)


# ── Шаг 2b: Вручную ──────────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_manual"))
async def cb_src_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(source_type="manual")
    await state.set_state(InviterFSM.users_manual)
    await _edit(
        callback,
        "✏️ Введите @username или user_id через запятую или с новой строки:\n\n"
        "<code>@user1, @user2\n123456789\n@user3</code>\n\n"
        "Максимум 500 пользователей за раз.",
        _cancel_kb(),
    )


@router.message(InviterFSM.users_manual)
async def msg_inviter_manual(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.mass_inviter_engine import parse_user_refs
    refs = parse_user_refs(message.text or "")
    if not refs:
        await message.answer("⚠️ Не удалось распознать пользователей. Введите @username или числовые ID.")
        return
    await state.update_data(user_refs=refs, total_users=len(refs))
    await state.set_state(InviterFSM.acc_count)
    data = await state.get_data()
    await _ask_acc_count_msg(message, data, len(refs), pool)


# ── Шаг 2c: По телефонам ─────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_phones"))
async def cb_src_phones(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(source_type="phones")
    await state.set_state(InviterFSM.phones)
    await _edit(
        callback,
        "📱 Введите номера телефонов через запятую или с новой строки:\n\n"
        "<code>+79991234567\n+7 999 123 45 67\n89991234567</code>\n\n"
        "Максимум 500 номеров.",
        _cancel_kb(),
    )


@router.message(InviterFSM.phones)
async def msg_inviter_phones(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.mass_inviter_engine import parse_phones
    phones = parse_phones(message.text or "")
    if not phones:
        await message.answer("⚠️ Не удалось распознать номера телефонов. Введите в формате +79991234567.")
        return
    await state.update_data(phones=phones, total_users=len(phones))
    await state.set_state(InviterFSM.acc_count)
    data = await state.get_data()
    await _ask_acc_count_msg(message, data, len(phones), pool)


# ── Шаг 3: Кол-во аккаунтов → подтверждение ─────────────────────────────────

async def _ask_acc_count(cb: CallbackQuery, data: dict, user_count: int, pool: asyncpg.Pool):
    total = await _acc_count(pool, cb.from_user.id)
    await _edit(
        cb,
        f"✅ Пользователей: <b>{user_count}</b>\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов задействовать? (0 = все):",
        _cancel_kb(),
    )


async def _ask_acc_count_msg(msg: Message, data: dict, user_count: int, pool: asyncpg.Pool):
    total = await _acc_count(pool, msg.from_user.id)
    await msg.answer(
        f"✅ Пользователей: <b>{user_count}</b>\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов задействовать? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(InviterFSM.acc_count)
async def msg_inviter_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    owner_id = message.from_user.id
    total = await _acc_count(pool, owner_id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    data = await state.get_data()
    group = data.get("group", "")
    source_type = data.get("source_type", "manual")
    total_users = data.get("total_users", 0)

    source_label = {
        "parser": f"База парсера ({total_users} чел.)",
        "manual": f"Вручную ({total_users} чел.)",
        "phones": f"По телефонам ({total_users} чел.)",
    }.get(source_type, str(source_type))

    # Распределяем пользователей по аккаунтам
    per_acc = max(1, (total_users + use - 1) // use)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=InviterCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        "👥 <b>Инвайтер — подтверждение</b>\n\n"
        f"🎯 Группа: <code>{html.escape(group)}</code>\n"
        f"📋 Источник: {html.escape(source_label)}\n"
        f"🔑 Аккаунтов: <b>{use}</b>\n"
        f"📊 ~{per_acc} пользователей на аккаунт",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Подтверждение и постановка в очередь ─────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "confirm"))
async def cb_inviter_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    acc_count = data.get("acc_count", 1)
    group = data.get("group", "")
    source_type = data.get("source_type", "manual")

    # Загрузить аккаунты
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

    # Собрать список пользователей для добавления
    if source_type == "parser":
        run_id = data.get("parse_run_id")
        if run_id:
            prows = await pool.fetch(
                "SELECT DISTINCT tg_user_id, username FROM parsed_audiences "
                "WHERE owner_id=$1 AND parse_run_id=$2 LIMIT 5000",
                owner_id, run_id,
            )
        else:
            prows = await pool.fetch(
                "SELECT DISTINCT tg_user_id, username FROM parsed_audiences "
                "WHERE owner_id=$1 LIMIT 5000",
                owner_id,
            )
        user_refs = [
            f"@{r['username']}" if r["username"] else str(r["tg_user_id"])
            for r in prows
        ]
        phones: list[str] = []
    elif source_type == "phones":
        user_refs = []
        phones = data.get("phones", [])
    else:
        user_refs = data.get("user_refs", [])
        phones = []

    total_users = len(user_refs) + len(phones)
    if total_users == 0:
        await callback.answer("⚠️ Список пользователей пуст", show_alert=True)
        return

    import json
    params = {
        "group": group,
        "account_ids": account_ids,
        "user_refs": user_refs,
        "phones": phones,
        "batch_size": 5,
    }
    label = f"Инвайтер: {group} ← {total_users} пользователей × {len(account_ids)} акк."
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'mass_invite','pending',$2,$3,$4) RETURNING id",
        owner_id, json.dumps(params), total_users, label,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=f"bm:op_detail:{op_id}")
    kb.button(text="◀️ В меню", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Инвайтер поставлен в очередь</b>\n\n"
        f"🆔 Операция: <b>#{op_id}</b>\n"
        f"🎯 Группа: <code>{html.escape(group)}</code>\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🔑 Аккаунтов: <b>{len(account_ids)}</b>",
        kb.as_markup(),
    )
