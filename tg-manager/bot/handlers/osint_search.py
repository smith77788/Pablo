"""Поиск аккаунтов по нику или почте — кнопка в разделе «Аудитория».

Поиск идёт по ~600 сайтам (модуль blackbird, `services/osint_search.py`) и
длится до пары минут, поэтому обработчик его не ждёт: ставит операцию
`osint_search` и сразу отвечает. Итог со ссылками на найденные профили
приходит обычным сообщением о завершении операции, полный список — в
«📋 Детали операции» и в мини-аппе.
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
from services import operation_bus
from services import osint_search as _osint

from bot.callbacks import BmCb, OsintCb

log = logging.getLogger(__name__)
router = Router(name="osint_search")


class OsintFSM(StatesGroup):
    query = State()


_INTRO = (
    "🕵️ <b>Поиск по нику</b>\n\n"
    "Ищет аккаунты человека по нику или почте примерно на 600 сайтах: "
    "соцсети, форумы, сервисы для разработчиков, игры, магазины.\n\n"
    "Поиск идёт в фоне до пары минут. Когда закончится, пришлю список "
    "найденных профилей со ссылками.\n\n"
    "Проверяются только открытые страницы сайтов, аккаунты Telegram не "
    "используются."
)


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        log.warning("osint_search: edit не удался: %s", e)
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти по нику или почте", callback_data=OsintCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="section", sub="audience"))
    kb.adjust(1)
    return kb.as_markup()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=OsintCb(action="menu"))
    return kb.as_markup()


@router.callback_query(OsintCb.filter(F.action == "menu"))
async def cb_osint_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = _INTRO
    if not _osint.is_available():
        text += "\n\n⚠️ Модуль поиска пока не установлен на сервере."
    await _edit(callback, text, _menu_kb())


@router.callback_query(OsintCb.filter(F.action == "start"))
async def cb_osint_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OsintFSM.query)
    await _edit(
        callback,
        "🕵️ <b>Кого ищем?</b>\n\n"
        "Пришлите один ник или одну почту:\n\n"
        "<code>johndoe</code>\n<code>@johndoe</code>\n<code>john@example.com</code>",
        _cancel_kb(),
    )


@router.message(OsintFSM.query)
async def msg_osint_query(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    try:
        kind, query = _osint.parse_query(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}. Попробуйте ещё раз.",
                             reply_markup=_cancel_kb())
        return
    await state.clear()
    if not _osint.is_available():
        await message.answer("⚠️ Модуль поиска пока не установлен на сервере.",
                             reply_markup=_menu_kb())
        return
    what = "нику" if kind == "username" else "почте"
    label = f"Поиск по {what}: {query}"[:120]
    try:
        op_id = await operation_bus.submit(
            pool, message.from_user.id, "osint_search",
            {"kind": kind, "query": query}, total_items=1, label=label)
    except PermissionError as exc:
        await message.answer(f"🔒 {html.escape(str(exc) or 'Нужна подписка')}",
                             reply_markup=_menu_kb())
        return
    except Exception:
        log.exception("osint_search: не удалось поставить поиск uid=%s",
                      message.from_user.id)
        await message.answer("⚠️ Не удалось запустить поиск, попробуйте позже.",
                             reply_markup=_menu_kb())
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="🔍 Искать ещё", callback_data=OsintCb(action="start"))
    kb.adjust(1)
    await message.answer(
        f"⏳ Ищу аккаунты по {what} <b>{html.escape(query)}</b>.\n"
        f"Операция #{op_id}. Итог пришлю сообщением, обычно это до пары минут.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
