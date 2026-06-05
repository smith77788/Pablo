"""Approval workflow для опасных массовых операций."""

from __future__ import annotations
import logging
import asyncpg
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import ApprovalCb

router = Router()
log = logging.getLogger(__name__)


def _approval_kb(op_id: int, confirmed: bool = False):
    kb = InlineKeyboardBuilder()
    if not confirmed:
        kb.button(
            text="✅ Подтвердить",
            callback_data=ApprovalCb(action="confirm", op_id=op_id),
        )
    kb.button(text="❌ Отмена", callback_data=ApprovalCb(action="cancel", op_id=op_id))
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(ApprovalCb.filter(F.action == "confirm"))
async def cb_approval_confirm(
    callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool
) -> None:
    op_id = callback_data.op_id
    try:
        op = await pool.fetchrow(
            "SELECT op_type, total_items, owner_id FROM operation_queue WHERE id=$1 AND status='waiting_approval'",
            op_id,
        )
    except Exception:
        await callback.answer("Ошибка при подтверждении операции.", show_alert=True)
        return
    if not op or op["owner_id"] != callback.from_user.id:
        await callback.answer("Операция не найдена или нет прав.", show_alert=True)
        return
    await callback.answer()
    try:
        await pool.execute(
            "UPDATE operation_queue SET requires_approval=FALSE, approved_at=now(), approved_by=$1, status='pending' WHERE id=$2 AND status='waiting_approval'",
            callback.from_user.id,
            op_id,
        )
    except Exception as exc:
        from html import escape
        await callback.message.edit_text(
            f"❌ <b>Ошибка подтверждения операции:</b>\n<code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return
    from bot.callbacks import BmCb

    _kb = InlineKeyboardBuilder()
    _kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
    _kb.adjust(1)
    if op:
        await callback.message.edit_text(
            f"✅ <b>Операция #{op_id} подтверждена</b>\n\n"
            f"Тип: <code>{op['op_type']}</code>\n"
            f"Целей: {op['total_items'] or '?'}\n\n"
            "Поставлена в очередь на выполнение.",
            parse_mode="HTML",
            reply_markup=_kb.as_markup(),
        )
    else:
        await callback.message.edit_text(
            "✅ Операция подтверждена и поставлена в очередь.",
            reply_markup=_kb.as_markup(),
        )


@router.callback_query(ApprovalCb.filter(F.action == "cancel"))
async def cb_approval_cancel(
    callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool
) -> None:
    op_id = callback_data.op_id
    try:
        op_check = await pool.fetchval(
            "SELECT owner_id FROM operation_queue WHERE id=$1 AND status='waiting_approval'",
            op_id,
        )
    except Exception:
        await callback.answer("Ошибка при отмене операции.", show_alert=True)
        return
    if not op_check or op_check != callback.from_user.id:
        await callback.answer("Операция не найдена или нет прав.", show_alert=True)
        return
    await callback.answer()
    try:
        await pool.execute(
            "UPDATE operation_queue SET status='cancelled' WHERE id=$1 AND status='waiting_approval'",
            op_id,
        )
    except Exception:
        pass
    from bot.callbacks import BmCb

    _kb = InlineKeyboardBuilder()
    _kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
    _kb.adjust(1)
    await callback.message.edit_text(
        f"❌ <b>Операция #{op_id} отменена.</b>",
        parse_mode="HTML",
        reply_markup=_kb.as_markup(),
    )
