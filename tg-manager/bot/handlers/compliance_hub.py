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
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 10


@router.callback_query(ComplianceCb.filter(F.action == "menu"))
async def cb_compliance_menu(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await safe_answer(callback)
    await state.clear()

    report = await compliance_engine.get_report(pool, callback.from_user.id, days=30)

    if not report:
        text = (
            "📋 <b>Compliance — аудит операций</b>\n\n"
            "<i>Нет данных за последние 30 дней.\n\n"
            "Все операции Infragram записываются с криптографической "
            "подписью (HMAC-SHA256) — это доказуемый и неизменяемый лог "
            "того что, когда и с каким результатом было сделано.</i>"
        )
    else:
        # Группы и подписи берём из движка: свой словарь здесь уже стоил
        # отчёту правды — считались исходы, которых никто не пишет, и в боте,
        # как и на экране, стояли нули при полном журнале.
        rate = str(report["success_rate"]).replace(".", ",")
        lines = [
            "📋 <b>Аудит операций</b>",
            "",
            "Период: <b>30 дней</b>",
            f"Всего записей: <b>{report['total']}</b>",
            f"Выполнено: <b>{report['ok']}</b> ({rate}% от оценённых)",
            f"Частично: <b>{report['partial']}</b>",
            f"Рисковых: <b>{report['risk']}</b>",
            f"Прочее (отмены, основания): <b>{report['neutral']}</b>",
            f"Типов операций: <b>{report['distinct_types']}</b>",
        ]
        by = report.get("by_outcome") or {}
        if by:
            lines.append("")
            lines.append("<b>По исходам:</b>")
            for outcome, n in sorted(by.items(), key=lambda kv: -kv[1]):
                lines.append(f"· {compliance_engine.outcome_ru(outcome)} — {n}")
        lines += ["", "<i>Каждая запись подписана HMAC-SHA256 — "
                      "гарантия целостности журнала.</i>"]
        text = "\n".join(lines)

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
    await safe_answer(callback)
    page   = callback_data.page
    offset = page * _PAGE_SIZE

    entries = await compliance_engine.get_recent(
        pool, callback.from_user.id, limit=_PAGE_SIZE, offset=offset
    )

    if not entries:
        text = "📜 <b>История операций</b>\n\n<i>Нет записей.</i>"
    else:
        lines = [f"📜 <b>История операций</b> (стр. {page + 1})\n"]
        for e in entries:
            # Подпись исхода — из движка, по-русски: свой список иконок знал
            # только исходы, которых продукт не пишет, и каждая запись
            # получала нейтральную точку.
            ru  = e.get("outcome_ru") or compliance_engine.outcome_ru(e["outcome"])
            ts  = e["created_at"].strftime("%d.%m %H:%M")
            op  = f" · операция №{e['op_id']}" if e.get("op_id") else ""
            lines.append(f"<code>{ts}</code> {e['op_type']} — {ru}{op}")
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
    await safe_answer(callback)

    report_text = await compliance_engine.export_text(pool, callback.from_user.id, days=30)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ComplianceCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"<pre>{report_text}</pre>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
