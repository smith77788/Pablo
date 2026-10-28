"""Проверка ресурсов на запрещённую тематику из бота (паритет с mini-app).

Resource Compliance Scan: read-only проверка списка ресурсов (каналы/группы) на
запрещённую тематику (CSAM/террор) через content_safety. Собирает доказательное
досье — НЕ постит, НЕ жалуется, НЕ сносит. Раньше запускалось только из mini-app
(compliance_scan_submit). Исполнитель op_worker._exec_compliance_scan уже есть.
"""
from __future__ import annotations

import logging
import re

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ComplianceScanCb, BmCb
from bot.utils.op_helpers import safe_answer, terminal_kb
from services import operation_bus

log = logging.getLogger(__name__)
router = Router()

_DEFAULT_ACC_COUNT = 2
_DEFAULT_PER_LIMIT = 50


class ComplianceScanFSM(StatesGroup):
    waiting_resources = State()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ComplianceScanCb(action="cancel"))
    return kb.as_markup()


async def _start(target, state: FSMContext) -> None:
    await state.set_state(ComplianceScanFSM.waiting_resources)
    text = (
        "🛡 <b>Проверка ресурсов на запрещёнку</b>\n\n"
        "Read-only проверка каналов/групп на запрещённую тематику "
        "(CSAM/террор). Собирает досье — ничего не постит и не сносит.\n\n"
        "Введите ресурсы через запятую или пробел (@username или ссылки), "
        "до 100 штук:"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_cancel_kb())


@router.message(Command("scan_resources"))
async def cmd_scan(message: Message, state: FSMContext) -> None:
    await _start(message, state)


@router.callback_query(ComplianceScanCb.filter(F.action == "open"))
async def cb_scan_open(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _start(callback, state)


@router.callback_query(ComplianceScanCb.filter(F.action == "cancel"))
async def cb_scan_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text("Проверка отменена.")


@router.message(ComplianceScanFSM.waiting_resources, F.text)
async def msg_scan_resources(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    raw = re.split(r"[\s,]+", message.text or "")
    resources = [c.strip().lstrip("@") for c in raw if c.strip()][:100]
    if not resources:
        await message.answer("⚠️ Укажите хотя бы один ресурс. Попробуйте /scan_resources снова.")
        return

    # Проверка активных аккаунтов (как в mini-app).
    try:
        has_acc = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "AND session_str IS NOT NULL",
            message.from_user.id,
        )
    except Exception:
        has_acc = 0
    if not has_acc:
        await message.answer(
            "⚠️ Нет активных аккаунтов — добавьте аккаунт в «📱 Аккаунты».",
            reply_markup=terminal_kb(),
        )
        return

    try:
        op_id = await operation_bus.submit(
            pool,
            message.from_user.id,
            "compliance_scan",
            {"resources": resources, "per_resource_limit": _DEFAULT_PER_LIMIT,
             "acc_count": _DEFAULT_ACC_COUNT},
            total_items=len(resources),
        )
    except Exception as exc:
        log.exception("compliance_scan submit uid=%s", message.from_user.id)
        await message.answer(f"⚠️ Не удалось поставить в очередь: {str(exc)[:150]}")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="🛡 Ещё проверка", callback_data=ComplianceScanCb(action="open"))
    kb.adjust(1)
    await message.answer(
        f"🛡 <b>Проверка поставлена в очередь</b>\n\n"
        f"Ресурсов: <b>{len(resources)}</b>\n"
        f"ID операции: <code>#{op_id}</code>\n\n"
        f"<i>Результат (досье по флагам) — в деталях операции / очереди.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
