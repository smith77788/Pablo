"""
Обработчик проверки даты регистрации/создания любого Telegram-профиля, канала, группы или бота.

Модуль регистрирует aiogram Router и описывает все хендлеры команд, callback-запросов
и FSM-состояний, связанных с фичей «дата регистрации».

Поддерживаемые входы:
  • Пересланное сообщение от пользователя / из канала / из группы / от бота
  • @username или t.me/xxx ссылка (публичные сущности)
  • t.me/+ или t.me/joinchat/ (приватные каналы/группы)
  • /regdate @username — прямой аргумент команды
  • Несколько сущностей — каждая на отдельной строке (батч до 10 штук)

Команды:
  /regdate — показывает справку или сразу проверяет, если передан аргумент
  /analyze — запускает полный многостраничный анализ сущности
  /follows — показывает список отслеживаемых объектов текущего пользователя

Callback-actions (через RegCb):
  menu, start, cancel — навигация по меню
  history            — постраничная история проверок пользователя
  exact              — принудительный запрос точной даты через Telethon
  analyze            — полный анализ (загружает данные, строит первую страницу)
  page               — переключение страницы внутри анализа (без перезагрузки)
  export             — скачать полный отчёт в виде .txt файла
  follow_toggle      — подписаться / отписаться от уведомлений об изменениях
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import time

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, RegCb
from bot.states import RegCheckFSM
from services import registration_checker as rc
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router(name="reg_checker")

_HELP_TEXT = (
    "🔍 <b>Дата регистрации / создания</b>\n\n"
    "Узнай когда был зарегистрирован любой Telegram-аккаунт "
    "или создан канал, группа, бот.\n\n"
    "<b>Как использовать:</b>\n"
    "• Перешли любое сообщение сюда\n"
    "• Отправь @username или ссылку t.me/...\n"
    "• Напиши числовой ID (например: <code>1234567890</code>)\n"
    "• Несколько на разных строках — батч-режим (до 10)\n\n"
    "📡 <i>Публичные каналы, боты и группы работают без привязки аккаунтов.\n"
    "Для приватных чатов и точной даты через первое сообщение — нужен аккаунт в пуле.</i>"
)


def _main_kb() -> object:
    """
    Строит главную инлайн-клавиатуру раздела «Дата регистрации».

    Содержит три кнопки: «Начать проверку», «История проверок» и «Меню».
    Возвращает объект InlineKeyboardMarkup, пригодный для передачи в reply_markup.

    Возвращает:
        InlineKeyboardMarkup — готовая клавиатура.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Начать проверку", callback_data=RegCb(action="start"))
    kb.button(text="📋 История проверок", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _waiting_kb() -> object:
    """
    Строит клавиатуру режима ожидания ввода сущности (FSM waiting_entity).

    Отображается пока бот ждёт от пользователя @username, ссылку или
    пересланное сообщение. Содержит единственную кнопку «Отмена»,
    позволяющую выйти из FSM-состояния без ввода.

    Возвращает:
        InlineKeyboardMarkup — клавиатура с кнопкой отмены.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=RegCb(action="cancel"))
    return kb.as_markup()


def _result_kb(entity_id: int, entity_type: str) -> object:
    """
    Строит клавиатуру после успешного получения полных данных о сущности.

    Отображается когда результат проверки уже включает метаданные (имя, тип и т.д.).
    Предлагает пользователю перейти к полному анализу, проверить ещё, открыть историю
    или вернуться в главное меню.

    Параметры:
        entity_id   — числовой Telegram ID сущности; передаётся в callback_data
                      для последующей загрузки полного анализа.
        entity_type — строка: 'user' | 'bot' | 'channel' | 'supergroup' | 'group';
                      передаётся в callback_data.

    Возвращает:
        InlineKeyboardMarkup — клавиатура с четырьмя кнопками.
    """
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔬 Полный анализ",
        callback_data=RegCb(action="analyze", entity_id=entity_id, entity_type=entity_type, page=0),
    )
    kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="📋 История", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _result_kb_retry_exact(entity_id: int, entity_type: str) -> object:
    """
    Строит клавиатуру для каналов/групп когда авто-экзакт не сработал.

    Используется вместо _result_kb() когда сущность является каналом или группой
    и фоновое обогащение ещё не получило точную дату через первое сообщение.
    Добавляет кнопку «Получить точную дату» для ручного запуска Telethon-запроса.

    Параметры:
        entity_id   — числовой Telegram ID канала/группы.
        entity_type — строка: 'channel' | 'supergroup' | 'group'.

    Возвращает:
        InlineKeyboardMarkup — клавиатура с четырьмя кнопками, включая «Точную дату».
    """
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📡 Получить точную дату",
        callback_data=RegCb(action="exact", entity_id=entity_id, entity_type=entity_type),
    )
    kb.button(
        text="🔬 Полный анализ",
        callback_data=RegCb(action="analyze", entity_id=entity_id, entity_type=entity_type, page=0),
    )
    kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _analyze_kb(entity_id: int, entity_type: str, current_page: int, is_following: bool = False) -> object:
    from services.entity_analyzer import PAGE_TITLES
    kb = InlineKeyboardBuilder()
    for page, title in PAGE_TITLES.items():
        if page == current_page:
            kb.button(
                text=f"› {title} ‹",
                callback_data=RegCb(action="page", entity_id=entity_id, entity_type=entity_type, page=page),
            )
        else:
            kb.button(
                text=title,
                callback_data=RegCb(action="page", entity_id=entity_id, entity_type=entity_type, page=page),
            )
    follow_label = "🔕 Отписаться" if is_following else "📌 Следить"
    kb.button(
        text=follow_label,
        callback_data=RegCb(action="follow_toggle", entity_id=entity_id, entity_type=entity_type, page=current_page),
    )
    kb.button(
        text="📋 Экспорт",
        callback_data=RegCb(action="export", entity_id=entity_id, entity_type=entity_type),
    )
    kb.button(
        text="🔄 Обновить",
        callback_data=RegCb(action="analyze", entity_id=entity_id, entity_type=entity_type, page=current_page),
    )
    kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
    kb.adjust(3, 3, 2, 1, 1)
    return kb.as_markup()


# ── /regdate command ──────────────────────────────────────────────────────────

@router.message(Command("regdate"))
async def cmd_regdate(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        await _handle_text_entity(message, pool, state, args[1].strip())
        return
    await message.answer(_HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb())


@router.message(Command("analyze"))
async def cmd_analyze(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        await _handle_text_entity(message, pool, state, args[1].strip())
        return
    await state.set_state(RegCheckFSM.waiting_entity)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=RegCb(action="cancel"))
    kb.adjust(1)
    await message.answer(
        "🔬 <b>Полный анализ Telegram-сущности</b>\n\n"
        "Перешли сообщение или отправь @username / t.me/... ссылку:\n\n"
        "<i>Поддерживаются: каналы, группы, пользователи, боты.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Menu callbacks ────────────────────────────────────────────────────────────

@router.callback_query(RegCb.filter(F.action == "menu"))
async def cb_reg_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(
            _HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb()
        )
    except Exception:
        await callback.message.answer(
            _HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb()
        )


@router.callback_query(RegCb.filter(F.action == "start"))
async def cb_reg_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(RegCheckFSM.waiting_entity)
    prompt = (
        "📨 <b>Перешли сообщение</b> или отправь @username / t.me/... ссылку:\n\n"
        "<i>Несколько сущностей — каждую на новой строке.\n"
        "Нажми ❌ Отмена чтобы выйти.</i>"
    )
    try:
        await callback.message.edit_text(
            prompt, parse_mode="HTML", reply_markup=_waiting_kb()
        )
    except Exception:
        await callback.message.answer(
            prompt, parse_mode="HTML", reply_markup=_waiting_kb()
        )


@router.callback_query(RegCb.filter(F.action == "cancel"))
async def cb_reg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(
            _HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb()
        )
    except Exception:
        pass


# ── History ───────────────────────────────────────────────────────────────────

@router.callback_query(RegCb.filter(F.action == "history"))
async def cb_reg_history(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    page = callback_data.page
    limit = 10
    offset = page * limit
    try:
        rows = await pool.fetch(
            """SELECT entity_id, entity_type, entity_name, username,
                      reg_date, method, checked_at,
                      participants_count, verified, scam
               FROM reg_check_cache
               WHERE checked_by=$1
               ORDER BY checked_at DESC
               LIMIT $2 OFFSET $3""",
            callback.from_user.id,
            limit,
            offset,
        )
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM reg_check_cache WHERE checked_by=$1",
            callback.from_user.id,
        ) or 0
    except Exception as e:
        log_exc_swallow(log, f"reg_history new-cols query failed, trying fallback: {e}")
        try:
            rows = await pool.fetch(
                """SELECT entity_id, entity_type, entity_name, username,
                          reg_date, method, checked_at
                   FROM reg_check_cache
                   WHERE checked_by=$1
                   ORDER BY checked_at DESC
                   LIMIT $2 OFFSET $3""",
                callback.from_user.id, limit, offset,
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM reg_check_cache WHERE checked_by=$1",
                callback.from_user.id,
            ) or 0
        except Exception:
            await callback.answer("Ошибка базы данных", show_alert=True)
            return

    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔍 Начать проверку", callback_data=RegCb(action="start"))
        kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
        kb.adjust(1)
        try:
            await callback.message.edit_text(
                "📋 <b>История проверок пуста.</b>\n\nЗапустите первую проверку.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            pass
        return

    type_icon = {
        "user": "👤", "bot": "🤖",
        "channel": "📢", "supergroup": "👥", "group": "👥",
    }
    lines = [f"📋 <b>История проверок</b> (всего: {total})\n"]
    for row in rows:
        icon = type_icon.get(row["entity_type"], "❓")
        name = html.escape(row["entity_name"] or "") or f"ID {row['entity_id']}"
        date_s = rc.format_date_ru(row["reg_date"]) if row["reg_date"] else "неизвестно"
        method_mark = "✅" if row["method"] == "first_message" else "📊"
        scam_mark = " ⛔" if row.get("scam") else ""
        verified_mark = " ✅" if row.get("verified") else ""
        pc = row.get("participants_count")
        participants_s = f" · {pc:,}👥".replace(",", " ") if pc else ""
        lines.append(
            f"{icon} {name}{verified_mark}{scam_mark}{participants_s} — "
            f"{date_s} {method_mark}"
        )

    text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    total_pages = max(1, (total + limit - 1) // limit)
    if page > 0:
        kb.button(text="◀️", callback_data=RegCb(action="history", page=page - 1))
    if total_pages > 1:
        kb.button(
            text=f"{page + 1}/{total_pages}",
            callback_data=RegCb(action="history", page=page),
        )
    if page < total_pages - 1:
        kb.button(text="▶️", callback_data=RegCb(action="history", page=page + 1))
    kb.button(text="🔍 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
    kb.adjust(3, 1, 1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        pass


# ── Exact date via Telethon (manual fallback) ─────────────────────────────────

@router.callback_query(RegCb.filter(F.action == "exact"))
async def cb_reg_exact(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Получаю точную дату...", show_alert=False)
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type or ""

    if entity_type not in ("channel", "supergroup", "group"):
        await callback.answer(
            "Точная дата доступна только для каналов и групп.", show_alert=True
        )
        return

    try:
        await callback.message.edit_text(
            "⏳ <b>Получаю точную дату создания...</b>\n"
            "<i>Запрашиваю первое сообщение канала через ваш аккаунт.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    canonical = rc.canonical_peer_id(entity_id)
    try:
        from telethon.tl.types import PeerChannel, PeerChat
        peer = (
            PeerChannel(canonical)
            if entity_type in ("channel", "supergroup")
            else PeerChat(canonical)
        )
    except ImportError:
        peer = canonical

    exact = await rc.get_channel_exact_date(pool, callback.from_user.id, peer)

    try:
        row = await pool.fetchrow(
            """SELECT entity_name, username, participants_count,
                      verified, scam, fake, premium, about
               FROM reg_check_cache WHERE entity_id=$1 AND entity_type=$2""",
            entity_id, entity_type,
        )
    except Exception:
        row = None

    name = row["entity_name"] if row else None
    uname = row["username"] if row else None
    estimate = rc.estimate_by_id(entity_id, entity_type)

    if exact:
        merged = {
            **estimate,
            "date": None,
            "exact_date": exact["date"],
            "method": "first_message",
        }
        if row:
            for col in ("participants_count", "verified", "scam", "fake", "premium", "about"):
                merged[col] = row.get(col)
        text = rc.format_result(merged, name, uname)
        await rc.cache_result(pool, callback.from_user.id, merged, name, uname)
        kb = _result_kb(entity_id, entity_type)
    else:
        if row:
            for col in ("participants_count", "verified", "scam", "fake", "premium", "about"):
                estimate[col] = row.get(col)
        text = rc.format_result(estimate, name, uname)
        text += (
            "\n\n⚠️ <i>Точную дату получить не удалось — "
            "нет доступных аккаунтов или аккаунт не имеет доступа к этому чату.</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
        kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
        kb.adjust(1)
        kb = kb.as_markup()

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


# ── FSM: incoming message in waiting_entity state ─────────────────────────────

@router.message(RegCheckFSM.waiting_entity, F.forward_origin)
async def fsm_reg_forwarded(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await _handle_forwarded(message, state, pool)


@router.message(RegCheckFSM.waiting_entity, F.text)
async def fsm_reg_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text = (message.text or "").strip()

    # Batch mode: multiple lines or comma-separated
    batch = rc.split_batch(text)
    if batch and any(rc.parse_link(item) for item in batch):
        await _handle_batch(message, pool, state, batch)
        return

    parsed = rc.parse_link(text)
    if not parsed:
        await message.answer(
            "❓ Не могу разобрать. Пришли @username, ссылку t.me/... или числовой ID.\n"
            "<i>Несколько сущностей — каждую на новой строке.</i>",
            parse_mode="HTML",
            reply_markup=_waiting_kb(),
        )
        return
    await _handle_text_entity(message, pool, state, text)


# ── Core logic ────────────────────────────────────────────────────────────────

async def _handle_forwarded(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    origin = message.forward_origin
    if origin is None:
        return

    entity_id: int | None = None
    entity_type: str | None = None
    name: str | None = None
    username: str | None = None

    if isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        entity_id = user.id
        entity_type = "bot" if user.is_bot else "user"
        name = user.full_name or ""
        username = user.username

    elif isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        entity_id = chat.id
        entity_type = "channel"
        name = chat.title or ""
        username = chat.username

    elif isinstance(origin, MessageOriginChat):
        chat = origin.sender_chat
        entity_id = chat.id
        ctype = chat.type
        if ctype == "supergroup":
            entity_type = "supergroup"
        elif ctype == "group":
            entity_type = "group"
        else:
            entity_type = "channel"
        name = chat.title or ""
        username = getattr(chat, "username", None)

    elif isinstance(origin, MessageOriginHiddenUser):
        await message.answer(
            f"🔒 <b>{html.escape(origin.sender_user_name or 'Скрытый пользователь')}</b>\n\n"
            "Этот пользователь скрыл свой профиль — "
            "Telegram не передаёт его ID в пересланных сообщениях.\n\n"
            "Попробуй найти его по @username если знаешь его.",
            parse_mode="HTML",
            reply_markup=_waiting_kb(),
        )
        return

    if entity_id is None or entity_type is None:
        await message.answer(
            "❓ Не удалось извлечь данные из пересланного сообщения.",
            reply_markup=_waiting_kb(),
        )
        return

    # For channels/groups: prefer resolving by username for full info
    peer = username or rc.canonical_peer_id(entity_id)
    if isinstance(peer, int) and entity_type in ("channel", "supergroup", "group"):
        try:
            from telethon.tl.types import PeerChannel, PeerChat
            peer = (
                PeerChannel(peer)
                if entity_type in ("channel", "supergroup")
                else PeerChat(peer)
            )
        except ImportError:
            pass

    await _show_result(
        message, pool, state, entity_id, entity_type, name, username,
        auto_resolve_peer=peer,
    )


async def _handle_text_entity(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    raw: str,
) -> None:
    parsed = rc.parse_link(raw)
    if not parsed:
        await message.answer(
            "❓ Не могу разобрать. Укажи @username, ссылку t.me/... или числовой ID.",
            reply_markup=_waiting_kb(),
        )
        return

    username_str = parsed["username"]

    # Numeric ID — estimate + try Telethon
    if parsed["type"] == "id":
        try:
            eid = int(username_str)
        except ValueError:
            await message.answer("❓ Некорректный ID.", reply_markup=_waiting_kb())
            return
        if eid < -1_000_000_000:
            etype = "channel"
        elif eid < 0:
            etype = "group"
        else:
            etype = "user"
        canonical = rc.canonical_peer_id(eid)
        try:
            from telethon.tl.types import PeerChannel, PeerChat
            if etype in ("channel", "supergroup"):
                peer = PeerChannel(canonical)
            elif etype == "group":
                peer = PeerChat(canonical)
            else:
                peer = canonical
        except ImportError:
            peer = canonical
        await _show_result(
            message, pool, state, eid, etype, None, None,
            auto_resolve_peer=peer,
        )
        return

    # Username / invite link — try Telethon first, Bot API as fallback
    loading = await message.answer(
        "⏳ <b>Запрашиваю данные...</b>",
        parse_mode="HTML",
        reply_markup=_waiting_kb(),
    )

    full_info = await rc.get_entity_full_info(pool, message.from_user.id, username_str)

    try:
        await loading.delete()
    except Exception:
        pass

    if full_info:
        await _show_result_from_full_info(message, pool, state, full_info)
        return

    # Telethon unavailable — try Bot API fallback (works for public channels, bots, groups)
    bot_entity_id: int | None = None
    bot_entity_type: str | None = None
    bot_name: str | None = None
    try:
        chat = await message.bot.get_chat(f"@{username_str.lstrip('@')}")
        bot_entity_id = chat.id
        bot_name = chat.title or chat.full_name or username_str
        ct = getattr(chat, "type", "")
        if ct == "channel":
            bot_entity_type = "channel"
        elif ct == "supergroup":
            bot_entity_type = "supergroup"
        elif ct == "group":
            bot_entity_type = "group"
        elif getattr(chat, "is_bot", False):
            bot_entity_type = "bot"
        else:
            bot_entity_type = "user"
    except Exception:
        pass

    if bot_entity_id is not None and bot_entity_type is not None:
        # Got ID from Bot API — pass username so _show_result can use Bot API for metadata
        await _show_result(
            message, pool, state,
            bot_entity_id, bot_entity_type, bot_name, username_str,
            auto_resolve_peer=username_str,
        )
        return

    # Complete failure — inform user
    await message.answer(
        f"⚠️ Не удалось найти <code>{html.escape(username_str)}</code>.\n\n"
        "Возможно, аккаунта/канала с таким username не существует.\n"
        "<i>Для приватных аккаунтов без username — используйте числовой ID или "
        "перешлите сообщение от этого человека.</i>",
        parse_mode="HTML",
        reply_markup=_waiting_kb(),
    )


async def _handle_batch(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    items: list[str],
) -> None:
    """Батч-режим: несколько сущностей → компактная сводная таблица."""
    await state.clear()

    loading = await message.answer(
        f"⏳ <b>Обрабатываю {len(items)} запросов...</b>",
        parse_mode="HTML",
    )

    lines: list[str] = [f"📊 <b>Результаты ({len(items)} сущностей)</b>\n"]

    for idx, item in enumerate(items, 1):
        parsed = rc.parse_link(item)
        if not parsed:
            lines.append(f"{idx}. ❓ <code>{html.escape(item)}</code> — не распознано")
            continue

        username_str = parsed["username"]

        if parsed["type"] == "id":
            try:
                eid = int(username_str)
            except ValueError:
                lines.append(f"{idx}. ❓ <code>{html.escape(item)}</code> — некорректный ID")
                continue
            if eid < -1_000_000_000:
                etype = "channel"
            elif eid < 0:
                etype = "group"
            else:
                etype = "user"
            result = rc.estimate_by_id(eid, etype)
            lines.append(rc.format_batch_line(idx, item, result))
            await rc.cache_result(pool, message.from_user.id, result, None, None)
        else:
            full_info = await rc.get_entity_full_info(
                pool, message.from_user.id, username_str
            )
            if full_info:
                estimate = rc.estimate_by_id(
                    full_info["entity_id"], full_info["entity_type"]
                )
                merged = {**estimate, **full_info}
                lines.append(rc.format_batch_line(idx, item, merged, full_info.get("name")))
                await rc.cache_result(
                    pool, message.from_user.id, merged,
                    full_info.get("name"), full_info.get("username"),
                )
            else:
                # Bot API fallback for public channels/bots/groups
                try:
                    chat = await message.bot.get_chat(f"@{username_str.lstrip('@')}")
                    b_id = chat.id
                    b_name = chat.title or chat.full_name or username_str
                    ct = getattr(chat, "type", "")
                    b_type = {"channel": "channel", "supergroup": "supergroup",
                              "group": "group"}.get(ct, "user")
                    est = rc.estimate_by_id(b_id, b_type)
                    est["name"] = b_name
                    lines.append(rc.format_batch_line(idx, item, est, b_name))
                    await rc.cache_result(pool, message.from_user.id, est, b_name, username_str)
                except Exception:
                    lines.append(rc.format_batch_line(idx, item, None))

    lines.append("")
    lines.append(
        "📡 <i>Нажми «Проверить ещё» для одиночного запроса с полными вкладками анализа.</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="📋 История", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)

    try:
        await loading.delete()
    except Exception:
        pass

    await message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


async def _bot_api_channel_info(bot, entity_id: int, peer) -> dict | None:
    """Базовые метаданные канала/группы через Bot API — не требует Telethon."""
    try:
        target: str | int
        if isinstance(peer, str):
            p = peer.strip()
            target = p if p.startswith("@") else f"@{p.lstrip('@')}"
        else:
            target = entity_id
        chat = await bot.get_chat(target)
        member_count: int | None = None
        try:
            member_count = await bot.get_chat_member_count(chat.id)
        except Exception:
            pass
        ct = getattr(chat, "type", "") or ""
        etype = {"channel": "channel", "supergroup": "supergroup", "group": "group"}.get(ct, "channel")
        return {
            "entity_id": chat.id,
            "entity_type": etype,
            "name": chat.title or "",
            "username": getattr(chat, "username", None),
            "about": getattr(chat, "description", "") or "",
            "participants_count": member_count,
            "verified": getattr(chat, "is_verified", False) or False,
            "scam": getattr(chat, "is_scam", False) or False,
            "fake": getattr(chat, "is_fake", False) or False,
            "_via_bot_api": True,
        }
    except Exception:
        return None


async def _show_result(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    entity_id: int,
    entity_type: str,
    name: str | None,
    username: str | None,
    auto_resolve_peer=None,
) -> None:
    """Показ результата: дата по ID — моментально. Метаданные — фоном."""
    await state.clear()

    estimate = rc.estimate_by_id(entity_id, entity_type)
    estimate["name"] = name
    estimate["username"] = username

    # Главное: показать дату сразу, без ожидания
    text = rc.format_result(estimate, name, username)
    is_channel = entity_type in ("channel", "supergroup", "group")
    kb = _result_kb(entity_id, entity_type) if not is_channel else _result_kb_retry_exact(entity_id, entity_type)
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as _html_err:
        if "parse entities" in str(_html_err).lower() or "can't parse" in str(_html_err).lower():
            import re as _re
            plain = _re.sub(r"<[^>]*>", "", text)
            await message.answer(plain, reply_markup=kb)
        else:
            raise
    await rc.cache_result(pool, message.from_user.id, estimate, name, username)

    # Фоновое обогащение: Telethon → Bot API — без блокировки
    peer = auto_resolve_peer or (username if username else rc.canonical_peer_id(entity_id))
    asyncio.create_task(
        _enrich_metadata(message, pool, entity_id, entity_type, name, username, peer)
    )


async def _enrich_metadata(
    message: Message,
    pool: asyncpg.Pool,
    entity_id: int,
    entity_type: str,
    name: str | None,
    username: str | None,
    peer,
) -> None:
    """Фоновое обогащение кэша: Telethon → Bot API. Не блокирует пользователя."""
    try:
        owner_id = message.from_user.id
        full_info = await rc.get_entity_full_info(pool, owner_id, peer)
        if not full_info and entity_type in ("channel", "supergroup", "group"):
            full_info = await _bot_api_channel_info(message.bot, entity_id, username or peer)
        if not full_info:
            return
        estimate = rc.estimate_by_id(entity_id, entity_type)
        merged = {**estimate, **full_info}
        await rc.cache_result(
            pool, owner_id, merged,
            full_info.get("name") or name,
            full_info.get("username") or username,
        )
    except Exception as e:
        log.debug("_enrich_metadata: %s", e)



async def _show_result_from_full_info(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    full_info: dict,
) -> None:
    """Показать результат когда Telethon уже вернул полные данные."""
    await state.clear()

    entity_id = full_info["entity_id"]
    entity_type = full_info["entity_type"]
    name = full_info.get("name")
    username = full_info.get("username")

    estimate = rc.estimate_by_id(entity_id, entity_type)
    merged = {**estimate, **full_info}

    text = rc.format_result(merged, name, username)
    kb = _result_kb(entity_id, entity_type)

    try:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as _html_err:
        if "parse entities" in str(_html_err).lower() or "can't parse" in str(_html_err).lower():
            import re as _re
            plain = _re.sub(r"<[^>]*>", "", text)
            await message.answer(plain, reply_markup=kb)
        else:
            raise
    await rc.cache_result(pool, message.from_user.id, merged, name, username)


# ── Full entity analysis ──────────────────────────────────────────────────────

_analysis_cache: dict[int, tuple[dict, float]] = {}
_CACHE_TTL = 600  # seconds


async def _get_or_fetch_analysis(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_id: int,
    entity_type: str,
) -> dict | None:
    cached = _analysis_cache.get(entity_id)
    if cached:
        data, ts = cached
        if time.time() - ts < _CACHE_TTL:
            return data

    from services import entity_analyzer as ea

    try:
        row = await pool.fetchrow(
            "SELECT username, entity_name, reg_date, method FROM reg_check_cache "
            "WHERE entity_id=$1 AND entity_type=$2",
            entity_id, entity_type,
        )
    except Exception:
        row = None

    # Prefer username; fall back to canonical positive ID (Telethon-safe)
    if row and row["username"]:
        peer = row["username"]
    else:
        peer = rc.canonical_peer_id(entity_id)

    if entity_type in ("channel", "supergroup", "group"):
        data = await ea.analyze_channel(pool, owner_id, peer)
    else:
        data = await ea.analyze_user(pool, owner_id, peer)

    # Fallback: build minimal data from cache when Telethon is unavailable
    if not data and row:
        est = rc.estimate_by_id(entity_id, entity_type)
        data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "title": row["entity_name"] or "",
            "name": row["entity_name"] or "",
            "username": row["username"],
            "description": "",
            "members": 0,
            "admins_count": 0,
            "bot_count": 0,
            "banned_count": 0,
            "online_count": None,
            "boost_level": 0,
            "created_at": row["reg_date"] or est["date"],
            "created_method": row["method"] or est["method"],
            "linked_chat_id": None,
            "linked_name": None,
            "slowmode_s": 0,
            "ttl": None,
            "noforwards": False,
            "verified": False,
            "scam": False,
            "fake": False,
            "restricted": False,
            "restriction_reason": [],
            "join_to_send": False,
            "join_request": False,
            "is_forum": False,
            "is_gigagroup": False,
            "has_signatures": False,
            "avg_views": 0,
            "avg_fwd": 0,
            "avg_react": 0,
            "avg_replies": 0,
            "max_views": 0,
            "engagement_rate": 0.0,
            "posts_per_day": 0.0,
            "peak_hour": None,
            "top_hashtags": [],
            "media_types": {},
            "avg_post_length": 0,
            "top_posts": [],
            "hour_dist": {},
            "admin_list": [],
            "seo_score": 0,
            "seo_notes": [
                "⚠️ Данные недоступны — добавьте аккаунт в пул для полного анализа"
            ],
            "posts_analyzed": 0,
            "bio": "",
            "phone": None,
            "premium": False,
            "is_contact": False,
            "is_mutual": False,
            "common_groups": 0,
            "photos_count": 0,
            "status": "неизвестен",
            "bot_info": {},
            "_partial": True,
        }

    if data:
        _analysis_cache[entity_id] = (data, time.time())
    return data


@router.callback_query(RegCb.filter(F.action == "analyze"))
async def cb_analyze(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Анализирую...")
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type or ""
    page = callback_data.page

    try:
        await callback.message.edit_text(
            "🔬 <b>Полный анализ</b>\n\n⏳ Получаю данные из Telegram...\n"
            "<i>Это может занять 10-30 секунд</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    data = await _get_or_fetch_analysis(
        pool, callback.from_user.id, entity_id, entity_type
    )
    if not data:
        try:
            await callback.message.edit_text(
                "❌ Не удалось получить данные.\n\n"
                "Убедитесь что в вашем пуле есть активный аккаунт.",
                parse_mode="HTML",
                reply_markup=_analyze_kb(entity_id, entity_type, page),
            )
        except Exception:
            pass
        return

    await _show_analysis_page(callback.message, data, entity_id, entity_type, page, pool, callback.from_user.id)


@router.callback_query(RegCb.filter(F.action == "page"))
async def cb_analyze_page(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type or ""
    page = callback_data.page

    data = await _get_or_fetch_analysis(
        pool, callback.from_user.id, entity_id, entity_type
    )
    if not data:
        await callback.answer(
            "❌ Данные устарели. Нажмите «Обновить».", show_alert=True
        )
        return

    await _show_analysis_page(callback.message, data, entity_id, entity_type, page, pool, callback.from_user.id)


@router.callback_query(RegCb.filter(F.action == "export"))
async def cb_analyze_export(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type or ""

    data = await _get_or_fetch_analysis(
        pool, callback.from_user.id, entity_id, entity_type
    )
    if not data:
        await callback.answer("❌ Нет данных для экспорта.", show_alert=True)
        return

    from services.entity_analyzer import format_export
    report = format_export(data)

    title = data.get("title") or data.get("name") or str(entity_id)
    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in title
    )[:30]
    buf = io.BytesIO(report.encode())
    buf.name = f"analysis_{safe_title}.txt"

    try:
        await callback.message.answer_document(
            buf,
            caption=f"📊 Полный отчёт: {html.escape(title)}",
            parse_mode="HTML",
        )
    except Exception:
        for i in range(0, min(len(report), 12000), 4000):
            await callback.message.answer(
                f"<code>{html.escape(report[i:i+4000])}</code>",
                parse_mode="HTML",
            )


async def _show_analysis_page(
    message: Message,
    data: dict,
    entity_id: int,
    entity_type: str,
    page: int,
    pool: asyncpg.Pool | None = None,
    owner_id: int | None = None,
) -> None:
    from services.entity_analyzer import PAGE_FORMATTERS
    from database import db as _db
    formatter = PAGE_FORMATTERS.get(page, PAGE_FORMATTERS[0])
    text = formatter(data)
    is_following = False
    if pool and owner_id:
        is_following = await _db.is_following(pool, owner_id, entity_id)
    kb = _analyze_kb(entity_id, entity_type, page, is_following=is_following)
    async def _send(txt: str, pm: str | None) -> None:
        try:
            await message.edit_text(txt, parse_mode=pm, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await message.answer(txt, parse_mode=pm, reply_markup=kb, disable_web_page_preview=True)

    try:
        await _send(text, "HTML")
    except Exception as _html_err:
        if "parse entities" in str(_html_err).lower() or "can't parse" in str(_html_err).lower():
            import re as _re
            plain = _re.sub(r"<[^>]*>", "", text)
            await _send(plain, None)
        else:
            raise


@router.callback_query(RegCb.filter(F.action == "follow_toggle"))
async def cb_follow_toggle(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    from database import db as _db
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type or "user"
    owner_id = callback.from_user.id
    currently_following = await _db.is_following(pool, owner_id, entity_id)
    if currently_following:
        await _db.unfollow_entity(pool, owner_id, entity_id)
        await callback.answer("🔕 Вы отписались от уведомлений", show_alert=False)
    else:
        await _db.follow_entity(pool, owner_id, entity_id, entity_type)
        await callback.answer("📌 Подписались! Уведомим при изменении имени/username", show_alert=True)
    # Re-render the current page with updated follow button
    data = await _get_or_fetch_analysis(pool, owner_id, entity_id, entity_type)
    if data:
        page = callback_data.page or 0
        await _show_analysis_page(callback.message, data, entity_id, entity_type, page, pool, owner_id)


# ── /follows command — list followed entities ──────────────────────────────

@router.message(Command("follows"))
async def cmd_follows(message: Message, pool: asyncpg.Pool) -> None:
    """Show list of entities this user is following."""
    from database import db as _db
    from services.registration_checker import format_date_ru
    owner_id = message.from_user.id
    follows = await _db.get_follows(pool, owner_id)
    if not follows:
        await message.answer(
            "📋 <b>Список слежений пуст</b>\n\n"
            "Нажмите кнопку <b>📌 Следить</b> в карточке анализа любого пользователя или канала.",
            parse_mode="HTML",
        )
        return

    lines = [f"📋 <b>Вы следите за {len(follows)} объектами:</b>\n"]
    for row in follows:
        eid = row["entity_id"]
        et = row["entity_type"]
        u = row["username"]
        n = row["display_name"]
        lbl = html.escape(row["label"] or "")
        icon = {"user": "👤", "bot": "🤖", "channel": "📢", "group": "👥", "supergroup": "👥"}.get(et, "❓")
        ref = f"@{html.escape(u)}" if u else f"<code>{eid}</code>"
        name_part = f" — {html.escape(n)}" if n else ""
        label_part = f" <i>({lbl})</i>" if lbl else ""
        kb_part = f"\n   /analyze_{eid}"
        lines.append(f"{icon} {ref}{name_part}{label_part}")
    lines.append(
        "\n<i>Вы получите уведомление при изменении username или имени.</i>"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")
