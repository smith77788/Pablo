"""Compliance Hub — cryptographic audit trail viewer and report generator."""

from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, ComplianceCb
from services import compliance_engine

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 10


@router.callback_query(ComplianceCb.filter(F.action == "menu"))
async def cb_compliance_menu(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()

    report = await compliance_engine.get_report(pool, callback.from_user.id, days=30)

    if not report:
        text = (
            "📋 <b>Compliance — аудит операций</b>\n\n"
            "<i>Нет данных за последние 30 дней.\n\n"
            "Все операции BotMother записываются с криптографической "
            "подписью (HMAC-SHA256) — это доказуемый и неизменяемый лог "
            "того что, когда и с каким результатом было сделано.</i>"
        )
    else:
        text = (
            f"📋 <b>Compliance — аудит операций</b>\n\n"
            f"Период: <b>30 дней</b>\n"
            f"Всего операций: <b>{report['total']}</b>\n"
            f"Успешных: <b>{report['ok']}</b> ({report['success_rate']}%)\n"
            f"Ошибок: <b>{report['errors']}</b>\n"
            f"FloodWait: <b>{report['floods']}</b>\n"
            f"Банов: <b>{report['bans']}</b>\n"
            f"Типов операций: <b>{report['distinct_types']}</b>\n"
            f"Аккаунтов: <b>{report['distinct_accounts']}</b>\n\n"
            f"<i>Каждая запись подписана HMAC-SHA256 — "
            f"гарантия целостности лога.</i>"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="📜 История операций", callback_data=ComplianceCb(action="history", page=0))
    kb.button(text="📄 Отчёт (текст)", callback_data=ComplianceCb(action="export"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ComplianceCb.filter(F.action == "history"))
async def cb_compliance_history(
    callback: CallbackQuery,
    callback_data: ComplianceCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    page   = callback_data.page
    offset = page * _PAGE_SIZE

    entries = await compliance_engine.get_recent(
        pool, callback.from_user.id, limit=_PAGE_SIZE, offset=offset
    )

    outcome_icons = {
        "success":    "✅",
        "flood_wait": "⏳",
        "error":      "❌",
        "ban":        "🚫",
        "unknown":    "·",
    }

    if not entries:
        text = "📜 <b>История операций</b>\n\n<i>Нет записей.</i>"
    else:
        lines = [f"📜 <b>История операций</b> (стр. {page + 1})\n"]
        for e in entries:
            icon = outcome_icons.get(e["outcome"], "·")
            ts   = e["created_at"].strftime("%d.%m %H:%M")
            acc  = f" acc:{e['account_id']}" if e.get("account_id") else ""
            op   = f" op:{e['op_id']}" if e.get("op_id") else ""
            lines.append(f"{icon} <code>{ts}</code> {e['op_type']}{acc}{op}")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(
            text="◀️ Назад",
            callback_data=ComplianceCb(action="history", page=page - 1),
        )
    if len(entries) == _PAGE_SIZE:
        kb.button(
            text="▶️ Вперёд",
            callback_data=ComplianceCb(action="history", page=page + 1),
        )
    kb.button(text="◀️ К отчёту", callback_data=ComplianceCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ComplianceCb.filter(F.action == "export"))
async def cb_compliance_export(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    report_text = await compliance_engine.export_text(pool, callback.from_user.id, days=30)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ComplianceCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"<pre>{report_text}</pre>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
