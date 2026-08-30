"""Глобальный поиск публичных сущностей Telegram из бота (паритет с mini-app).

Раньше global_search (Telethon contacts.SearchRequest) был доступен только в
mini-app. Здесь — инлайн-поиск реальным аккаунтом владельца: /search или кнопка,
ввод запроса → результаты сразу (без очереди), тем же движком global_search_engine.
"""
from __future__ import annotations

import asyncio
import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import GSearchCb
from bot.utils.op_helpers import safe_answer, terminal_kb
from services.security import sanitize_search_query

log = logging.getLogger(__name__)
router = Router()

_TYPE_ICON = {"channel": "📢", "group": "👥", "user": "👤", "bot": "🤖"}


class GlobalSearchFSM(StatesGroup):
    waiting_query = State()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GSearchCb(action="cancel"))
    return kb.as_markup()


async def _prompt(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GlobalSearchFSM.waiting_query)
    text = (
        "🔎 <b>Глобальный поиск Telegram</b>\n\n"
        "Введите запрос — имя канала, группы, бота или @username.\n"
        "Поиск выполняется реальным аккаунтом, результат сразу."
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_cancel_kb())


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    await _prompt(message, state)


@router.callback_query(GSearchCb.filter(F.action == "open"))
async def cb_search_open(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _prompt(callback, state)


@router.callback_query(GSearchCb.filter(F.action == "cancel"))
async def cb_search_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text("Поиск отменён.")


@router.message(GlobalSearchFSM.waiting_query, F.text)
async def msg_search_query(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    query = sanitize_search_query(message.text or "")
    if not query:
        await message.answer("⚠️ Пустой запрос. Попробуйте /search снова.")
        return

    try:
        # Одна дверь: лучший аккаунт через флуд-осознанный выбор.
        from services import resource_selector as _rsel
        acc = await _rsel.select_account(pool, message.from_user.id, action_type="parse", min_trust_score=0.0)
    except Exception as e:
        log.warning("global_search acc fetch uid=%s: %s", message.from_user.id, e)
        acc = None
    if not acc or not acc.get("session_str"):
        await message.answer(
            "⚠️ Нет активного аккаунта для поиска — добавьте аккаунт в «📱 Аккаунты».",
            reply_markup=terminal_kb(),
        )
        return

    status = await message.answer("🔎 Ищу…")
    from services import global_search_engine as gse

    try:
        res = await asyncio.wait_for(
            gse.search_public(acc["session_str"], query, 20, _acc=dict(acc)),
            timeout=40,
        )
    except asyncio.TimeoutError:
        await status.edit_text("⚠️ Аккаунт не ответил за 40с — проверьте прокси/сессию.")
        return
    except Exception as exc:
        log.exception("global_search uid=%s q=%r", message.from_user.id, query)
        await status.edit_text(f"⚠️ Ошибка: {html.escape(str(exc)[:140])}")
        return

    if not res.get("ok"):
        await status.edit_text(f"⚠️ {html.escape(str(res.get('error') or 'Поиск не выполнен'))}")
        return

    results = res.get("results") or []
    if not results:
        await status.edit_text(f"🔎 По запросу «{html.escape(query)}» ничего не найдено.")
        return

    lines = [f"🔎 <b>Результаты по «{html.escape(query)}»</b>\n"]
    for r in results[:20]:
        icon = _TYPE_ICON.get(r.get("type"), "•")
        title = html.escape(str(r.get("title") or "—")[:60])
        uname = r.get("username")
        uname_s = f" @{html.escape(uname)}" if uname else ""
        badges = ""
        if r.get("verified"):
            badges += " ✅"
        if r.get("scam"):
            badges += " ⚠️scam"
        parts = r.get("participants")
        parts_s = f" · 👥{parts:,}" if isinstance(parts, int) else ""
        lines.append(f"{icon} <b>{title}</b>{uname_s}{badges}{parts_s}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Новый поиск", callback_data=GSearchCb(action="open"))
    kb.adjust(1)
    await status.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )
