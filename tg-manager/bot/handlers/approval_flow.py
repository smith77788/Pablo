"""Approval workflow для опасных массовых операций."""
from __future__ import annotations
import logging
import asyncpg
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import ApprovalCb
from database import db

router = Router()
log = logging.getLogger(__name__)


def _approval_kb(op_id: int, confirmed: bool = False):
    kb = InlineKeyboardBuilder()
    if not confirmed:
        kb.button(text="✅ Подтвердить", callback_data=ApprovalCb(action="confirm", op_id=op_id))
    kb.button(text="❌ Отмена", callback_data=ApprovalCb(action="cancel", op_id=op_id))
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(ApprovalCb.filter(F.action == "confirm"))
async def cb_approval_confirm(callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    op_id = callback_data.op_id
    # Approve the operation
    await pool.execute(
        "UPDATE operation_queue SET requires_approval=FALSE, approved_at=now(), approved_by=$1, status='pending' WHERE id=$2 AND status='waiting_approval'",
        callback.from_user.id, op_id
    )
    op = await pool.fetchrow("SELECT op_type, total_items FROM operation_queue WHERE id=$1", op_id)
    if op:
        await callback.message.edit_text(
            f"✅ <b>Операция #{op_id} подтверждена</b>\n\n"
            f"Тип: <code>{op['op_type']}</code>\n"
            f"Целей: {op['total_items'] or '?'}\n\n"
            "Поставлена в очередь на выполнение.",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text("✅ Операция подтверждена и поставлена в очередь.")


@router.callback_query(ApprovalCb.filter(F.action == "cancel"))
async def cb_approval_cancel(callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    op_id = callback_data.op_id
    await pool.execute(
        "UPDATE operation_queue SET status='cancelled' WHERE id=$1 AND status='waiting_approval'",
        op_id
    )
    await callback.message.edit_text(
        f"❌ <b>Операция #{op_id} отменена.</b>",
        parse_mode="HTML"
    )
