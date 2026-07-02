"""Social Graph Engine UI — audience overlaps and channel relationship map."""

from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, GraphCb
from services import graph_engine

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(GraphCb.filter(F.action == "menu"))
async def cb_graph_menu(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()

    stats = await graph_engine.get_node_stats(pool)

    text = (
        "🌐 <b>Граф аудитории</b>\n\n"
        f"Узлов в графе: <b>{stats['nodes']}</b>\n"
        f"Связей: <b>{stats['edges']}</b>\n"
        f"Сильных пересечений (>10%): <b>{stats['strong_overlaps']}</b>\n\n"
        "<i>Граф накапливается пассивно из операций парсинга, "
        "Content Mesh и мониторинга. Обновляется каждые 6 часов.</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Пересечения аудиторий", callback_data=GraphCb(action="overlaps"))
    kb.button(text="📡 Мои каналы в графе",    callback_data=GraphCb(action="my_nodes"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="analytics"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GraphCb.filter(F.action == "overlaps"))
async def cb_graph_overlaps(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    callback_data: GraphCb,
) -> None:
    await callback.answer()

    page     = callback_data.page
    per_page = 8
    overlaps = await graph_engine.get_top_overlaps(pool, limit=per_page * (page + 1))

    if not overlaps:
        text = (
            "🌐 <b>Пересечения аудиторий</b>\n\n"
            "<i>Нет данных. Система накапливает пересечения по мере "
            "работы парсера и Infrastructure Radar.</i>\n\n"
            "Чем больше операций вы выполняете — тем точнее карта."
        )
    else:
        page_data = overlaps[page * per_page : (page + 1) * per_page]
        lines = ["🌐 <b>Топ пересечений аудиторий</b>\n"]
        for o in page_data:
            a = o.get("title_a") or o.get("id_a") or "?"
            b = o.get("title_b") or o.get("id_b") or "?"
            pct = round(float(o["overlap_pct"]) * 100, 1)
            shared = int(o["shared_users"])
            lines.append(
                f"• <b>{pct}%</b> — {a} ↔ {b}\n"
                f"  <i>Общих участников: {shared}</i>"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(
            text="◀️",
            callback_data=GraphCb(action="overlaps", page=page - 1),
        )
    if len(overlaps) > (page + 1) * per_page:
        kb.button(
            text="▶️",
            callback_data=GraphCb(action="overlaps", page=page + 1),
        )
    kb.button(text="◀️ Назад", callback_data=GraphCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GraphCb.filter(F.action == "my_nodes"))
async def cb_graph_my_nodes(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    nodes = await graph_engine.get_user_nodes(pool, callback.from_user.id)

    if not nodes:
        text = (
            "📡 <b>Мои каналы в графе</b>\n\n"
            "<i>Ваши каналы ещё не появились в графе.\n"
            "Создайте Content Mesh — каналы будут автоматически добавлены.</i>"
        )
    else:
        lines = ["📡 <b>Мои каналы в графе</b>\n"]
        for n in nodes:
            title   = n.get("title") or n.get("username") or n.get("entity_id") or "?"
            members = int(n.get("member_count") or 0)
            lines.append(f"• <b>{title}</b> — {members:,} участников")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GraphCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
