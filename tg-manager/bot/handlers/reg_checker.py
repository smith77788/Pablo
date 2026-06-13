"""
Проверка даты регистрации/создания любого Telegram-профиля, канала, группы или бота.

Поддерживаемые входы:
  • Пересланное сообщение от пользователя / из канала / из группы / от бота
  • @username или t.me/xxx ссылка (публичные сущности)
  • t.me/+ или t.me/joinchat/ (приватные каналы/группы)
  • /regdate @username — прямой аргумент команды
  • Несколько сущностей — каждая на отдельной строке (батч до 10 штук)

Команда: /regdate
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
    "<i>Для каналов и групп дата создания определяется автоматически "
    "через первое сообщение (требует активный аккаунт в вашем пуле).</i>"
)


def _main_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Начать проверку", callback_data=RegCb(action="start"))
    kb.button(text="📋 История проверок", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _waiting_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=RegCb(action="cancel"))
    return kb.as_markup()


def _result_kb(entity_id: int, entity_type: str) -> object:
    """Клавиатура после успешного получения полных данных."""
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
    """Клавиатура когда авто-экзакт не сработал — показываем ручную кнопку."""
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
        callback_data=RegCb(action="follow_toggle", entity_id=entity_id, entity_type=entity_type),
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

    # Username / invite link — get full info via Telethon
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

    if not full_info:
        await message.answer(
            f"⚠️ Не удалось разрешить <code>{html.escape(username_str)}</code>.\n\n"
            "Возможно:\n"
            "• Аккаунта/канала с таким username не существует\n"
            "• У вас нет активных аккаунтов в пуле\n"
            "• Приватный канал — нужно быть участником",
            parse_mode="HTML",
            reply_markup=_waiting_kb(),
        )
        return

    await _show_result_from_full_info(message, pool, state, full_info)


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
    """
    Двухфазный показ для каналов/групп:
    фаза 1 — ID-оценка сразу; фаза 2 — Telethon точные данные → редактирование.
    """
    await state.clear()

    estimate = rc.estimate_by_id(entity_id, entity_type)
    estimate["name"] = name
    estimate["username"] = username

    is_channel = entity_type in ("channel", "supergroup", "group")

    if is_channel:
        text_loading = rc.format_result(estimate, name, username)
        text_loading += "\n\n⏳ <i>Получаю точную дату и метаданные...</i>"
        sent = await message.answer(text_loading, parse_mode="HTML")

        peer = auto_resolve_peer or rc.canonical_peer_id(entity_id)
        full_info = await rc.get_entity_full_info(pool, message.from_user.id, peer)

        if full_info:
            merged = {**estimate, **full_info}
            final_name = name or full_info.get("name")
            final_username = username or full_info.get("username")
            text = rc.format_result(merged, final_name, final_username)
            kb = _result_kb(entity_id, entity_type)
            await rc.cache_result(
                pool, message.from_user.id, merged, final_name, final_username
            )
        else:
            text = rc.format_result(estimate, name, username)
            text += (
                "\n\n⚠️ <i>Метаданные недоступны — нет аккаунтов в пуле или "
                "канал приватный. Оценка только по ID.</i>"
            )
            kb = _result_kb_retry_exact(entity_id, entity_type)
            await rc.cache_result(pool, message.from_user.id, estimate, name, username)

        try:
            await sent.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

    else:
        text = rc.format_result(estimate, name, username)
        kb = _result_kb(entity_id, entity_type)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        await rc.cache_result(pool, message.from_user.id, estimate, name, username)

        if auto_resolve_peer:
            asyncio.create_task(
                _enrich_user_metadata(
                    pool, message.from_user.id, entity_id, entity_type, auto_resolve_peer
                )
            )


async def _enrich_user_metadata(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_id: int,
    entity_type: str,
    peer,
) -> None:
    """Фоновое обогащение кэша метаданными пользователя/бота."""
    try:
        full_info = await rc.get_entity_full_info(pool, owner_id, peer)
        if not full_info:
            return
        estimate = rc.estimate_by_id(entity_id, entity_type)
        merged = {**estimate, **full_info}
        await rc.cache_result(
            pool, owner_id, merged,
            full_info.get("name"), full_info.get("username"),
        )
    except Exception as e:
        log.debug("_enrich_user_metadata: %s", e)


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

    await message.answer(text, parse_mode="HTML", reply_markup=kb)
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
                "\u26a0\ufe0f \u0414\u0430\u043d\u043d\u044b\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b \u2014 \u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0432 \u043f\u0443\u043b \u0434\u043b\u044f \u043f\u043e\u043b\u043d\u043e\u0433\u043e \u0430\u043d\u0430\u043b\u0438\u0437\u0430"
            ],
            "posts_analyzed": 0,
            "bio": "",
            "phone": None,
            "premium": False,
            "is_contact": False,
            "is_mutual": False,
            "common_groups": 0,
            "photos_count": 0,
            "status": "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d",
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
    try:
        await message.edit_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
    except Exception:
        await message.answer(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )


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
