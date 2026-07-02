"""Репортер — массовые жалобы на пользователей, каналы и сообщения.

Режимы:
  • Жалоба на профиль/канал (ReportPeer)
  • Жалоба на сообщения (Report по msg_id)
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

from bot.callbacks import ReporterCb, BmCb
from services.reporter_engine import REPORT_REASONS

log = logging.getLogger(__name__)
router = Router()


class ReporterFSM(StatesGroup):
    target = State()
    msg_ids = State()     # только для report_msg
    reason = State()      # ожидаем нажатие кнопки
    report_text = State()
    acc_count = State()


async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ReporterCb(action="menu"))
    return kb.as_markup()


def _reason_kb(mode: str):
    kb = InlineKeyboardBuilder()
    for key, (label, _) in REPORT_REASONS.items():
        kb.button(text=label, callback_data=ReporterCb(action=f"reason_{key}", sub=mode))
    kb.button(text="❌ Отмена", callback_data=ReporterCb(action="menu"))
    kb.adjust(2)
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

@router.callback_query(ReporterCb.filter(F.action == "menu"))
async def cb_reporter_menu(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    total = await _acc_count(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Жалоба на профиль / канал", callback_data=ReporterCb(action="start_peer"))
    kb.button(text="💬 Жалоба на сообщения", callback_data=ReporterCb(action="start_msg"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    await _edit(
        callback,
        "🚨 <b>Репортер</b>\n\n"
        "Массовые жалобы через все ваши аккаунты.\n\n"
        "Причины: спам, насилие, порнография, наркотики, фейк, личные данные.\n\n"
        f"🔑 Доступно аккаунтов: <b>{total}</b>",
        kb.as_markup(),
    )


# ── Жалоба на профиль / канал ─────────────────────────────────────────────────

@router.callback_query(ReporterCb.filter(F.action == "start_peer"))
async def cb_start_peer(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="peer")
    await state.set_state(ReporterFSM.target)
    await _edit(
        callback,
        "👤 <b>Жалоба на профиль / канал</b>\n\n"
        "Введите @username или ссылку t.me/...",
        _cancel_kb(),
    )


@router.message(ReporterFSM.target)
async def msg_reporter_target(message: Message, state: FSMContext) -> None:
    from services.reporter_engine import parse_target_ref
    target = parse_target_ref(message.text or "")
    if not target:
        await message.answer("⚠️ Не удалось распознать цель.")
        return
    data = await state.get_data()
    mode = data.get("mode", "peer")
    await state.update_data(target=target)

    if mode == "msg":
        await state.set_state(ReporterFSM.msg_ids)
        await message.answer(
            f"📌 Канал: <code>{html.escape(target)}</code>\n\n"
            "Введите ID сообщений через запятую:",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
    else:
        await message.answer(
            f"📌 Цель: <code>{html.escape(target)}</code>\n\n"
            "Выберите причину жалобы:",
            parse_mode="HTML",
            reply_markup=_reason_kb(mode),
        )


# ── Жалоба на сообщения ───────────────────────────────────────────────────────

@router.callback_query(ReporterCb.filter(F.action == "start_msg"))
async def cb_start_msg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="msg")
    await state.set_state(ReporterFSM.target)
    await _edit(
        callback,
        "💬 <b>Жалоба на сообщения</b>\n\n"
        "Введите @username канала/группы:",
        _cancel_kb(),
    )


@router.message(ReporterFSM.msg_ids)
async def msg_reporter_msg_ids(message: Message, state: FSMContext) -> None:
    from services.boost_engine import parse_msg_ids
    ids = parse_msg_ids(message.text or "")
    if not ids:
        await message.answer("⚠️ Введите ID сообщений через запятую: 123, 124")
        return
    await state.update_data(msg_ids=ids)
    await message.answer(
        f"✅ Сообщений: <b>{len(ids)}</b>\n\nВыберите причину жалобы:",
        parse_mode="HTML",
        reply_markup=_reason_kb("msg"),
    )


# ── Выбор причины ─────────────────────────────────────────────────────────────

@router.callback_query(ReporterCb.filter(F.action.startswith("reason_")))
async def cb_pick_reason(
    callback: CallbackQuery, callback_data: ReporterCb, state: FSMContext
) -> None:
    reason_key = callback_data.action.replace("reason_", "")
    await state.update_data(reason=reason_key)
    await state.set_state(ReporterFSM.report_text)
    reason_label = REPORT_REASONS.get(reason_key, ("Другое", ""))[0]
    await _edit(
        callback,
        f"Причина: <b>{html.escape(reason_label)}</b>\n\n"
        "Введите текст жалобы (или отправьте <code>-</code> для пропуска):",
        _cancel_kb(),
    )


@router.message(ReporterFSM.report_text)
async def msg_reporter_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text = (message.text or "").strip()
    if text == "-":
        text = ""
    await state.update_data(report_text=text)
    total = await _acc_count(pool, message.from_user.id)
    await state.set_state(ReporterFSM.acc_count)
    await message.answer(
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько задействовать? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(ReporterFSM.acc_count)
async def msg_reporter_acc_count(
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
    target = data.get("target", "")
    mode = data.get("mode", "peer")
    reason = data.get("reason", "spam")
    reason_label = REPORT_REASONS.get(reason, ("?", ""))[0]

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить жалобы", callback_data=ReporterCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=ReporterCb(action="menu"))
    kb.adjust(2)
    mode_label = "Профиль/канал" if mode == "peer" else f"Сообщения ({len(data.get('msg_ids', []))} шт.)"
    await message.answer(
        "🚨 <b>Репортер — подтверждение</b>\n\n"
        f"🎯 Цель: <code>{html.escape(target)}</code>\n"
        f"📋 Режим: {mode_label}\n"
        f"⚠️ Причина: {html.escape(reason_label)}\n"
        f"🔑 Аккаунтов: <b>{use}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Подтверждение ─────────────────────────────────────────────────────────────

@router.callback_query(ReporterCb.filter(F.action == "confirm"))
async def cb_reporter_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    acc_count = data.get("acc_count", 1)

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

    import json
    params = {
        "mode": data.get("mode", "peer"),
        "target": data.get("target", ""),
        "reason": data.get("reason", "spam"),
        "report_text": data.get("report_text", ""),
        "msg_ids": data.get("msg_ids", []),
        "account_ids": account_ids,
    }
    reason_label = REPORT_REASONS.get(params["reason"], ("?", ""))[0]
    label = f"Жалобы: {params['target']} [{reason_label}] × {len(account_ids)} акк."
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'mass_report','pending',$2,$3,$4) RETURNING id",
        owner_id, json.dumps(params), len(account_ids), label,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ В меню", callback_data=ReporterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Жалобы поставлены в очередь</b>\n\n"
        f"🆔 Операция: <b>#{op_id}</b>\n"
        f"{html.escape(label)}",
        kb.as_markup(),
    )
