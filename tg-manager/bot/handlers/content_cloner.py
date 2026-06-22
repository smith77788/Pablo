"""Контент-клонер — копирование/пересылка сообщений между каналами."""
from __future__ import annotations

import html
import logging
import re

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ContentClonerCb, BmCb

log = logging.getLogger(__name__)
router = Router()


class ClonerFSM(StatesGroup):
    source = State()        # шаг 1: ввод источника
    targets = State()       # шаг 2: ввод целей (по одной)
    msg_count = State()     # шаг 3: сколько сообщений (если не указаны вручную)
    acc_count = State()     # шаг 4 (не используется — всегда 1 аккаунт)


_COUNT_OPTIONS = [5, 10, 20, 50]
_MODE_LABELS = {"forward": "🔄 Переслать (с подписью источника)", "copy": "📋 Скопировать (без подписи)"}


# ── Главное меню ──────────────────────────────────────────────────────────────


@router.callback_query(ContentClonerCb.filter(F.action == "menu"))
async def cb_cloner_menu(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    text = (
        "<b>📋 Контент-клонер</b>\n\n"
        "Копирует или пересылает сообщения из канала-источника в один или несколько каналов-целей.\n\n"
        "<b>Режимы:</b>\n"
        "• <b>Переслать</b> — пересылает с подписью «Из канала X»\n"
        "• <b>Скопировать</b> — скачивает контент и постит заново без attribution\n\n"
        "Выберите режим клонирования:"
    )
    kb = InlineKeyboardBuilder()
    for mode_key, label in _MODE_LABELS.items():
        kb.button(text=label, callback_data=ContentClonerCb(action="set_source", sub=mode_key))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


# ── Шаг 1: ввод источника ────────────────────────────────────────────────────


@router.callback_query(ContentClonerCb.filter(F.action == "set_source"))
async def cb_cloner_set_source(cb: CallbackQuery, callback_data: ContentClonerCb, state: FSMContext) -> None:
    mode = callback_data.sub or "forward"
    await state.update_data(mode=mode, targets=[])
    await state.set_state(ClonerFSM.source)

    mode_label = _MODE_LABELS.get(mode, mode)
    text = (
        f"<b>📋 Контент-клонер</b> — {mode_label}\n\n"
        "Шаг 1: Введите <b>источник</b> — канал откуда клонировать сообщения:\n\n"
        "<i>Форматы: @username, t.me/username, числовой ID</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@router.message(ClonerFSM.source)
async def msg_cloner_source(msg: Message, state: FSMContext) -> None:
    from services.content_cloner_engine import parse_channel_ref
    raw = (msg.text or "").strip()
    if not raw:
        await msg.answer("⚠️ Введите ссылку или username канала.")
        return
    source_ref = parse_channel_ref(raw)
    await state.update_data(source_ref=source_ref)
    await state.set_state(ClonerFSM.targets)

    text = (
        f"<b>📋 Источник:</b> <code>{html.escape(source_ref)}</code>\n\n"
        "Шаг 2: Введите <b>каналы-цели</b> (куда клонировать).\n"
        "Вводите по одному или через запятую:\n\n"
        "<i>@channel1, @channel2, t.me/channel3</i>\n\n"
        "Когда введёте все цели — нажмите <b>Далее</b>."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Далее — выбор количества", callback_data=ContentClonerCb(action="set_count"))
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    kb.adjust(1)
    await msg.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ── Шаг 2: ввод целей ────────────────────────────────────────────────────────


@router.message(ClonerFSM.targets)
async def msg_cloner_targets(msg: Message, state: FSMContext) -> None:
    from services.content_cloner_engine import parse_channel_ref
    raw = (msg.text or "").strip()
    if not raw:
        await msg.answer("⚠️ Введите username или ссылку канала.")
        return

    data = await state.get_data()
    targets: list[str] = data.get("targets", [])

    for part in re.split(r"[,;\n]+", raw):
        ref = parse_channel_ref(part.strip())
        if ref and ref not in targets:
            targets.append(ref)

    await state.update_data(targets=targets)
    count = len(targets)
    targets_str = "\n".join(f"  • <code>{html.escape(t)}</code>" for t in targets[:20])
    text = (
        f"<b>📋 Цели ({count} шт.):</b>\n{targets_str}\n\n"
        "Добавьте ещё каналы или нажмите <b>Далее</b>."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Далее — выбор количества", callback_data=ContentClonerCb(action="set_count"))
    kb.button(text="🗑 Очистить список", callback_data=ContentClonerCb(action="clear_targets"))
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    kb.adjust(1)
    await msg.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(ContentClonerCb.filter(F.action == "clear_targets"))
async def cb_cloner_clear_targets(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(targets=[])
    text = (
        "<b>🗑 Список целей очищен.</b>\n\n"
        "Введите каналы заново."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


# ── Шаг 3: количество сообщений ──────────────────────────────────────────────


@router.callback_query(ContentClonerCb.filter(F.action == "set_count"))
async def cb_cloner_set_count(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    targets = data.get("targets", [])
    if not targets:
        await cb.answer("⚠️ Добавьте хотя бы один канал-цель.", show_alert=True)
        return

    text = (
        "<b>📋 Количество сообщений</b>\n\n"
        "Сколько последних сообщений клонировать из источника?\n\n"
        "<i>Выберите или введите вручную (1–200):</i>"
    )
    kb = InlineKeyboardBuilder()
    for n in _COUNT_OPTIONS:
        kb.button(text=str(n), callback_data=ContentClonerCb(action="confirm", sub=str(n)))
    kb.button(text="✏️ Ввести вручную", callback_data=ContentClonerCb(action="custom_count"))
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    kb.adjust(4, 1, 1)
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@router.callback_query(ContentClonerCb.filter(F.action == "custom_count"))
async def cb_cloner_custom_count(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ClonerFSM.msg_count)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    try:
        await cb.message.edit_text(
            "Введите количество сообщений (1–200):",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await cb.message.answer("Введите количество сообщений (1–200):", reply_markup=kb.as_markup())
    await cb.answer()


@router.message(ClonerFSM.msg_count)
async def msg_cloner_count(msg: Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 200):
        await msg.answer("⚠️ Введите число от 1 до 200.")
        return
    count = int(raw)
    await _show_confirm(msg, state, count, via_message=True)


# ── Шаг 4: подтверждение и запуск ────────────────────────────────────────────


@router.callback_query(ContentClonerCb.filter(F.action == "confirm"))
async def cb_cloner_confirm(cb: CallbackQuery, callback_data: ContentClonerCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    sub = callback_data.sub
    if sub.isdigit():
        count = int(sub)
        await _do_queue(cb, state, pool, count)
    else:
        await cb.answer("⚠️ Неверный параметр.", show_alert=True)


async def _show_confirm(msg_or_cb, state: FSMContext, count: int, via_message: bool = False) -> None:
    data = await state.get_data()
    source_ref = data.get("source_ref", "?")
    targets: list[str] = data.get("targets", [])
    mode = data.get("mode", "forward")
    mode_label = "Пересылка (с подписью)" if mode == "forward" else "Копирование (без подписи)"
    targets_str = "\n".join(f"  • <code>{html.escape(t)}</code>" for t in targets[:10])
    if len(targets) > 10:
        targets_str += f"\n  <i>... и ещё {len(targets) - 10}</i>"

    text = (
        "<b>📋 Подтверждение клонирования</b>\n\n"
        f"<b>Источник:</b> <code>{html.escape(source_ref)}</code>\n"
        f"<b>Количество:</b> последние {count} сообщений\n"
        f"<b>Режим:</b> {mode_label}\n"
        f"<b>Цели ({len(targets)}):</b>\n{targets_str}\n\n"
        "<i>Будет выполнено через очередь операций.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=ContentClonerCb(action="confirm", sub=str(count)))
    kb.button(text="❌ Отмена", callback_data=ContentClonerCb(action="menu"))
    kb.adjust(1)

    if via_message:
        await msg_or_cb.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        try:
            await msg_or_cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            await msg_or_cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        await msg_or_cb.answer()


async def _do_queue(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, msg_count: int) -> None:
    data = await state.get_data()
    await state.clear()

    owner_id = cb.from_user.id
    source_ref: str = data.get("source_ref", "")
    targets: list[str] = data.get("targets", [])
    mode: str = data.get("mode", "forward")

    if not source_ref or not targets:
        await cb.answer("⚠️ Нет источника или целей.", show_alert=True)
        return

    # Выбрать один аккаунт с наибольшим trust_score
    acc_row = await pool.fetchrow(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        owner_id,
    )
    if not acc_row:
        await cb.answer("⚠️ Нет доступных аккаунтов", show_alert=True)
        return

    account_ids = [acc_row["id"]]

    op_params = {
        "source_ref": source_ref,
        "target_refs": targets,
        "mode": mode,
        "msg_ids": [],
        "msg_count": msg_count,
        "account_ids": account_ids,
    }

    import json
    op_id = await pool.fetchval(
        """INSERT INTO operation_queue
           (owner_id, op_type, params, status, total_items, done_items, created_at)
           VALUES ($1, 'content_clone', $2::jsonb, 'pending', $3, 0, NOW())
           RETURNING id""",
        owner_id, json.dumps(op_params), len(targets),
    )

    mode_label = "переслать" if mode == "forward" else "скопировать"
    await cb.message.edit_text(
        f"✅ <b>Клонирование поставлено в очередь</b> (#{op_id})\n\n"
        f"📋 Источник: <code>{html.escape(source_ref)}</code>\n"
        f"📨 Режим: {mode_label} последние {msg_count} сообщений → {len(targets)} канал(ов)\n\n"
        "<i>Следите за статусом в разделе «Операции».</i>",
        parse_mode="HTML",
    )
    await cb.answer("✅ В очереди!")
