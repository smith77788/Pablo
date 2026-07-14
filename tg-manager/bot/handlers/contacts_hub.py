"""Contacts Hub из бота (паритет с mini-app uch_*).

Единый реестр контактов (unified_contacts) со своими движками
(services/contacts_hub/*) был доступен только из mini-app. Здесь — бот-сторона
для самых ценных операций: обзор, поиск, применение авто-тегов, пересчёт графа
связей, переключение «избранное». Вызывает те же движки, что и mini_app_api.
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ContactsHubCb, BmCb
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_SEARCH_LIMIT = 12


class ContactsHubFSM(StatesGroup):
    waiting_query = State()


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Обзор", callback_data=ContactsHubCb(action="stats"))
    kb.button(text="🔍 Поиск контактов", callback_data=ContactsHubCb(action="search"))
    kb.button(text="🏷 Применить авто-теги", callback_data=ContactsHubCb(action="smart_tags"))
    kb.button(text="🕸 Пересчитать граф связей", callback_data=ContactsHubCb(action="graph"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(2, 1, 1, 1)
    return kb


async def _show_menu(target, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    text = (
        "🗂 <b>Contacts Hub</b>\n\n"
        "Единый реестр контактов со всех аккаунтов: поиск, авто-теги, "
        "граф связей, избранное."
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=_menu_kb().as_markup())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_menu_kb().as_markup())


@router.message(Command("contacts"))
async def cmd_contacts(message: Message, state: FSMContext) -> None:
    await _show_menu(message, state)


@router.callback_query(ContactsHubCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _show_menu(callback, state)


@router.callback_query(ContactsHubCb.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    from services.contacts_hub.stats_engine import get_full_stats

    try:
        s = await get_full_stats(pool, callback.from_user.id)
    except Exception as exc:
        log.warning("contacts_hub stats uid=%s: %s", callback.from_user.id, exc)
        await callback.message.edit_text(
            "⚠️ Не удалось получить статистику.", reply_markup=_back_kb().as_markup()
        )
        return
    await callback.message.edit_text(
        "📊 <b>Обзор контактов</b>\n\n"
        f"Всего: <b>{s.get('total', 0)}</b>\n"
        f"⭐ Избранных: <b>{s.get('favorites', 0)}</b>\n"
        f"💎 Premium: <b>{s.get('premium', 0)}</b>\n"
        f"@ С username: <b>{s.get('with_username', 0)}</b>\n"
        f"📞 С телефоном: <b>{s.get('with_phone', 0)}</b>\n"
        f"🏷 Тегов: <b>{s.get('tags', 0)}</b> · 👥 Групп: <b>{s.get('groups', 0)}</b>\n"
        f"📝 С заметками: <b>{s.get('notes', 0)}</b>",
        parse_mode="HTML", reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(ContactsHubCb.filter(F.action == "smart_tags"))
async def cb_smart_tags(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Применяю авто-теги…")
    from services.contacts_hub.smart_tags_engine import apply_smart_tags

    try:
        res = await apply_smart_tags(pool, callback.from_user.id)
    except Exception as exc:
        log.warning("contacts_hub smart_tags uid=%s: %s", callback.from_user.id, exc)
        await callback.message.edit_text(
            "⚠️ Не удалось применить авто-теги.", reply_markup=_back_kb().as_markup()
        )
        return
    await callback.message.edit_text(
        "🏷 <b>Авто-теги применены</b>\n\n"
        f"Проставлено тегов: <b>{res.get('applied', 0)}</b>\n"
        f"Проверено правил: <b>{res.get('rules_checked', 0)}</b>",
        parse_mode="HTML", reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(ContactsHubCb.filter(F.action == "graph"))
async def cb_graph(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Пересчитываю граф связей…")
    from services.contacts_hub.relationship_engine import compute_relationships

    try:
        res = await compute_relationships(pool, callback.from_user.id)
    except Exception as exc:
        log.warning("contacts_hub graph uid=%s: %s", callback.from_user.id, exc)
        await callback.message.edit_text(
            "⚠️ Не удалось пересчитать граф.", reply_markup=_back_kb().as_markup()
        )
        return
    await callback.message.edit_text(
        "🕸 <b>Граф связей пересчитан</b>\n\n"
        f"Связей найдено: <b>{res.get('count', 0)}</b>\n\n"
        "<i>Связь = контакты, встречающиеся у одних и тех же аккаунтов.</i>",
        parse_mode="HTML", reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(ContactsHubCb.filter(F.action == "search"))
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(ContactsHubFSM.waiting_query)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ContactsHubCb(action="menu"))
    await callback.message.edit_text(
        "🔍 <b>Поиск контактов</b>\n\n"
        "Введите имя, @username, телефон или тег (можно операторы "
        "<code>tag:vip</code>, <code>@username</code>):",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(ContactsHubFSM.waiting_query, F.text)
async def msg_search_query(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    query = (message.text or "").strip()
    await state.update_data(query=query)
    await _render_search(message, pool, message.from_user.id, query)


async def _render_search(target, pool, owner_id: int, query: str) -> None:
    from services.contacts_hub.search_engine import search_contacts

    try:
        results = await search_contacts(pool, owner_id, query, limit=_SEARCH_LIMIT)
    except Exception as exc:
        log.warning("contacts_hub search uid=%s: %s", owner_id, exc)
        results = []

    if not results:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔍 Ещё поиск", callback_data=ContactsHubCb(action="search"))
        kb.button(text="◀️ В меню", callback_data=ContactsHubCb(action="menu"))
        kb.adjust(1)
        text = f"🔍 По запросу «{html.escape(query)}» ничего не найдено."
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=kb.as_markup())
        else:
            await target.answer(text, reply_markup=kb.as_markup())
        return

    lines = [f"🔍 <b>Найдено: {len(results)}</b> (запрос «{html.escape(query)}»)\n"]
    kb = InlineKeyboardBuilder()
    for r in results:
        name = " ".join(filter(None, [r.get("first_name"), r.get("last_name")])) or "—"
        uname = r.get("username")
        uname_s = f" @{html.escape(uname)}" if uname else ""
        fav = "⭐" if r.get("is_favorite") else ""
        tags = r.get("tags") or []
        tags_s = f" · 🏷 {', '.join(html.escape(str(t)) for t in tags[:3])}" if tags else ""
        lines.append(f"{fav}<b>{html.escape(name)}</b>{uname_s}{tags_s}")
        btn = "⭐ Убрать" if r.get("is_favorite") else "☆ В избранное"
        kb.button(text=f"{btn}: {name[:14]}",
                  callback_data=ContactsHubCb(action="fav", cid=str(r.get("id"))))
    kb.button(text="🔍 Ещё поиск", callback_data=ContactsHubCb(action="search"))
    kb.button(text="◀️ В меню", callback_data=ContactsHubCb(action="menu"))
    kb.adjust(1)
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML",
                                       reply_markup=kb.as_markup(), disable_web_page_preview=True)
    else:
        await target.answer(text, parse_mode="HTML",
                            reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.callback_query(ContactsHubCb.filter(F.action == "fav"))
async def cb_toggle_fav(
    callback: CallbackQuery, callback_data: ContactsHubCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    cid = callback_data.cid
    owner_id = callback.from_user.id
    from services.contacts_hub.bulk_ops_engine import bulk_set_favorite

    try:
        cur = await pool.fetchval(
            "SELECT is_favorite FROM unified_contacts WHERE id=$1 AND owner_id=$2",
            cid, owner_id,
        )
    except Exception as exc:
        log.warning("contacts_hub fav read uid=%s cid=%s: %s", owner_id, cid, exc)
        await callback.answer("Контакт не найден.", show_alert=True)
        return
    if cur is None:
        await callback.answer("Контакт не найден.", show_alert=True)
        return
    try:
        await bulk_set_favorite(pool, owner_id, [cid], not cur)
    except Exception as exc:
        log.warning("contacts_hub fav toggle uid=%s cid=%s: %s", owner_id, cid, exc)
        await callback.answer("Ошибка переключения.", show_alert=True)
        return
    await callback.answer("⭐ В избранном" if not cur else "Убрано из избранного")
    # Перерисовать текущий поиск с обновлённым состоянием.
    data = await state.get_data()
    query = data.get("query")
    if query is not None:
        await _render_search(callback, pool, owner_id, query)


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data=ContactsHubCb(action="menu"))
    return kb
