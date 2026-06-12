"""
Проверка даты регистрации/создания любого Telegram-профиля, канала, группы или бота.

Поддерживаемые входы:
  • Пересланное сообщение от пользователя / из канала / из группы / от бота
  • @username или t.me/xxx ссылка (публичные сущности)
  • t.me/+ или t.me/joinchat/ (приватные каналы/группы)
  • /regdate @username — прямой аргумент команды

Команда: /regdate
Меню: через RegCb(action="menu")
"""
from __future__ import annotations

import asyncio
import html
import logging

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
    "• Напиши числовой ID (например: <code>1234567890</code>)\n\n"
    "<i>Для каналов и групп доступна точная дата через первое сообщение "
    "(требует активный аккаунт в вашем пуле).</i>"
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


def _result_kb(entity_id: int, entity_type: str, can_exact: bool) -> object:
    kb = InlineKeyboardBuilder()
    if can_exact:
        kb.button(
            text="📡 Узнать точную дату (через аккаунт)",
            callback_data=RegCb(action="exact", entity_id=entity_id, entity_type=entity_type),
        )
    kb.button(
        text="🔬 Полный анализ",
        callback_data=RegCb(action="analyze", entity_id=entity_id, entity_type=entity_type, page=0),
    )
    kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="📋 История", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _analyze_kb(entity_id: int, entity_type: str, current_page: int) -> object:
    from services.entity_analyzer import PAGE_TITLES
    kb = InlineKeyboardBuilder()
    for page, title in PAGE_TITLES.items():
        if page == current_page:
            kb.button(text=f"› {title} ‹", callback_data=RegCb(action="page", entity_id=entity_id, entity_type=entity_type, page=page))
        else:
            kb.button(text=title, callback_data=RegCb(action="page", entity_id=entity_id, entity_type=entity_type, page=page))
    kb.button(text="📋 Экспорт", callback_data=RegCb(action="export", entity_id=entity_id, entity_type=entity_type))
    kb.button(text="🔄 Обновить", callback_data=RegCb(action="analyze", entity_id=entity_id, entity_type=entity_type, page=current_page))
    kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
    kb.adjust(3, 3, 2, 1)
    return kb.as_markup()


# ── /regdate command ──────────────────────────────────────────────────────────

@router.message(Command("regdate"))
async def cmd_regdate(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        # Direct argument: /regdate @username or /regdate https://t.me/xxx
        await _handle_text_entity(message, pool, state, args[1].strip())
        return
    await message.answer(_HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb())


# ── /analyze command — alias that goes directly to full analysis mode ─────────

_ANALYZE_HELP_TEXT = (
    "🔬 <b>Полный анализатор сущностей</b>\n\n"
    "Получи исчерпывающий анализ любого Telegram-канала, группы, пользователя или бота:\n"
    "• 📊 Обзор (ID, дата создания, описание)\n"
    "• 📈 Статистика (охваты, ER, частота постов)\n"
    "• 📝 Контент (типы медиа, хэштеги, топ-посты)\n"
    "• 🔗 Сеть и связи\n"
    "• 🔍 SEO-оценка с рекомендациями\n"
    "• 👮 Администраторы\n\n"
    "<b>Как использовать:</b>\n"
    "• Перешли любое сообщение сюда\n"
    "• Отправь @username или ссылку t.me/...\n\n"
    "<i>Требует активный аккаунт в вашем пуле для загрузки данных.</i>"
)


@router.message(Command("analyze"))
async def cmd_analyze(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Alias for /regdate but goes directly to full analysis input mode."""
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        # Direct argument: /analyze @username
        await _handle_text_entity(message, pool, state, args[1].strip())
        return
    # No argument — show analyze-specific help and set FSM to waiting state
    await state.set_state(RegCheckFSM.waiting_entity)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=RegCb(action="cancel"))
    kb.adjust(1)
    await message.answer(_ANALYZE_HELP_TEXT, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Menu callbacks ────────────────────────────────────────────────────────────

@router.callback_query(RegCb.filter(F.action == "menu"))
async def cb_reg_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(_HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb())
    except Exception:
        await callback.message.answer(_HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb())


@router.callback_query(RegCb.filter(F.action == "start"))
async def cb_reg_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(RegCheckFSM.waiting_entity)
    try:
        await callback.message.edit_text(
            "📨 <b>Перешли сообщение</b> или отправь @username / t.me/... ссылку:\n\n"
            "<i>Нажми ❌ Отмена чтобы выйти.</i>",
            parse_mode="HTML",
            reply_markup=_waiting_kb(),
        )
    except Exception:
        await callback.message.answer(
            "📨 <b>Перешли сообщение</b> или отправь @username / t.me/... ссылку:",
            parse_mode="HTML",
            reply_markup=_waiting_kb(),
        )


@router.callback_query(RegCb.filter(F.action == "cancel"))
async def cb_reg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(_HELP_TEXT, parse_mode="HTML", reply_markup=_main_kb())
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
            """SELECT entity_id, entity_type, entity_name, username, reg_date, method, checked_at
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
        log_exc_swallow(log, f"reg_history query: {e}")
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

    lines = ["📋 <b>История проверок</b>\n"]
    type_icon = {"user": "👤", "bot": "🤖", "channel": "📢", "supergroup": "👥", "group": "👥"}
    for row in rows:
        icon = type_icon.get(row["entity_type"], "❓")
        name = html.escape(row["entity_name"] or "") or f"ID {row['entity_id']}"
        if row["reg_date"]:
            date_str = rc.format_date_ru(row["reg_date"])
        else:
            date_str = "неизвестно"
        method_short = "✅" if row["method"] == "first_message" else "📊"
        lines.append(f"{icon} {name} — {date_str} {method_short}")

    text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    total_pages = (total + limit - 1) // limit
    if page > 0:
        kb.button(text="◀️", callback_data=RegCb(action="history", page=page - 1))
    if total_pages > 1:
        kb.button(text=f"{page + 1}/{total_pages}", callback_data=RegCb(action="history", page=page))
    if page < total_pages - 1:
        kb.button(text="▶️", callback_data=RegCb(action="history", page=page + 1))
    kb.button(text="🔍 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
    kb.adjust(3, 1, 1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        pass


# ── Exact date via Telethon ───────────────────────────────────────────────────

@router.callback_query(RegCb.filter(F.action == "exact"))
async def cb_reg_exact(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Получаю точную дату...", show_alert=False)
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type

    if entity_type not in ("channel", "supergroup", "group"):
        await callback.answer(
            "Точная дата доступна только для каналов и групп.", show_alert=True
        )
        return

    # Show loading state
    try:
        await callback.message.edit_text(
            "⏳ <b>Получаю точную дату создания...</b>\n\n"
            "<i>Запрашиваю первое сообщение канала через ваш аккаунт.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Resolve peer for Telethon
    canonical = rc.canonical_peer_id(entity_id)
    try:
        from telethon.tl.types import PeerChannel, PeerChat
        if entity_type in ("channel", "supergroup"):
            peer = PeerChannel(canonical)
        else:
            peer = PeerChat(canonical)
    except ImportError:
        peer = canonical

    exact = await rc.get_channel_exact_date(pool, callback.from_user.id, peer)
    if not exact:
        # Telethon failed — show cached estimate
        try:
            row = await pool.fetchrow(
                "SELECT entity_name, username, reg_date, method FROM reg_check_cache "
                "WHERE entity_id=$1 AND entity_type=$2",
                entity_id, entity_type,
            )
        except Exception:
            row = None
        name = row["entity_name"] if row else None
        uname = row["username"] if row else None
        estimate = rc.estimate_by_id(entity_id, entity_type)
        text = rc.format_result(estimate, name, uname)
        text += "\n\n⚠️ <i>Точную дату получить не удалось — нет доступных аккаунтов или аккаунт не имеет доступа к этому чату.</i>"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
        kb.button(text="◀️ Назад", callback_data=RegCb(action="menu"))
        kb.adjust(1)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            pass
        return

    # Merge exact date with cached entity info
    try:
        row = await pool.fetchrow(
            "SELECT entity_name, username FROM reg_check_cache WHERE entity_id=$1 AND entity_type=$2",
            entity_id, entity_type,
        )
    except Exception:
        row = None
    name = row["entity_name"] if row else None
    uname = row["username"] if row else None

    merged = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "date": exact["date"],
        "method": "first_message",
        "confidence": "exact",
    }
    text = rc.format_result(merged, name, uname)

    # Update cache with exact date
    await rc.cache_result(pool, callback.from_user.id, merged, name, uname)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить ещё", callback_data=RegCb(action="start"))
    kb.button(text="📋 История", callback_data=RegCb(action="history"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        pass


# ── FSM: incoming message in waiting_entity state ─────────────────────────────

@router.message(RegCheckFSM.waiting_entity, F.forward_origin)
async def fsm_reg_forwarded(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await _handle_forwarded(message, state, pool)


@router.message(RegCheckFSM.waiting_entity, F.text)
async def fsm_reg_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = (message.text or "").strip()
    parsed = rc.parse_link(text)
    if not parsed:
        await message.answer(
            "❓ Не могу разобрать. Пришли @username, ссылку t.me/... или числовой ID.",
            reply_markup=_waiting_kb(),
        )
        return
    await _handle_text_entity(message, pool, state, parsed["username"])


# ── Core logic helpers ─────────────────────────────────────────────────────────

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
        if ctype in ("supergroup",):
            entity_type = "supergroup"
        elif ctype in ("group",):
            entity_type = "group"
        else:
            entity_type = "channel"
        name = chat.title or ""
        username = getattr(chat, "username", None)

    elif isinstance(origin, MessageOriginHiddenUser):
        # User hid their profile — no ID available
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

    await _show_result(message, pool, state, entity_id, entity_type, name, username)


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

    # Numeric ID — use estimate only (no way to know type without resolve)
    if parsed["type"] == "id":
        try:
            eid = int(username_str)
        except ValueError:
            await message.answer("❓ Некорректный ID.", reply_markup=_waiting_kb())
            return
        if eid < -1_000_000_000:
            etype = "channel"   # -100XXXXXXXXX → channel/supergroup
        elif eid < 0:
            etype = "group"     # small negative → old group
        else:
            etype = "user"      # positive → user or bot
        await _show_result(message, pool, state, eid, etype, None, None)
        return

    # @username or invite link — resolve via Telethon
    loading = await message.answer(
        "⏳ Разрешаю username...", reply_markup=_waiting_kb()
    )
    resolved = await rc.resolve_username(pool, message.from_user.id, username_str)

    if not resolved:
        try:
            await loading.edit_text(
                f"⚠️ Не удалось разрешить <code>{html.escape(username_str)}</code>.\n\n"
                "Возможно:\n"
                "• Аккаунта/канала с таким username не существует\n"
                "• У вас нет активных аккаунтов в пуле\n"
                "• Приватный канал (нужно быть участником)",
                parse_mode="HTML",
                reply_markup=_waiting_kb(),
            )
        except Exception:
            pass
        return

    try:
        await loading.delete()
    except Exception:
        pass

    await _show_result(
        message, pool, state,
        resolved["entity_id"], resolved["entity_type"],
        resolved.get("name"), resolved.get("username"),
    )


async def _show_result(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    entity_id: int,
    entity_type: str,
    name: str | None,
    username: str | None,
) -> None:
    await state.clear()

    result = rc.estimate_by_id(entity_id, entity_type)
    text = rc.format_result(result, name, username)

    # Cache the estimate
    await rc.cache_result(pool, message.from_user.id, result, name, username)

    can_exact = entity_type in ("channel", "supergroup", "group")
    kb = _result_kb(entity_id, entity_type, can_exact)

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Full entity analysis ──────────────────────────────────────────────────────

# In-memory analysis cache to avoid re-fetching on tab switch (ttl=10min)
_analysis_cache: dict[int, tuple[dict, float]] = {}
_CACHE_TTL = 600  # seconds


async def _get_or_fetch_analysis(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_id: int,
    entity_type: str,
) -> dict | None:
    import time
    cache_key = entity_id
    cached = _analysis_cache.get(cache_key)
    if cached:
        data, ts = cached
        if time.time() - ts < _CACHE_TTL:
            return data

    from services import entity_analyzer as ea

    # Resolve peer from cache or by ID
    row = await pool.fetchrow(
        "SELECT username FROM reg_check_cache WHERE entity_id=$1 AND entity_type=$2",
        entity_id, entity_type,
    )
    peer = (row["username"] if row and row["username"] else None) or entity_id

    if entity_type in ("channel", "supergroup", "group"):
        data = await ea.analyze_channel(pool, owner_id, peer)
    else:
        data = await ea.analyze_user(pool, owner_id, peer)

    if data:
        _analysis_cache[cache_key] = (data, time.time())
    return data


@router.callback_query(RegCb.filter(F.action == "analyze"))
async def cb_analyze(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Анализирую...")
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type
    page = callback_data.page

    try:
        await callback.message.edit_text(
            "🔬 <b>Полный анализ</b>\n\n⏳ Получаю данные из Telegram...\n"
            "<i>Это может занять 10-30 секунд</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    data = await _get_or_fetch_analysis(pool, callback.from_user.id, entity_id, entity_type)
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

    await _show_analysis_page(callback.message, data, entity_id, entity_type, page)


@router.callback_query(RegCb.filter(F.action == "page"))
async def cb_analyze_page(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type
    page = callback_data.page

    data = await _get_or_fetch_analysis(pool, callback.from_user.id, entity_id, entity_type)
    if not data:
        await callback.answer("❌ Данные устарели. Нажмите «Обновить».", show_alert=True)
        return

    await _show_analysis_page(callback.message, data, entity_id, entity_type, page)


@router.callback_query(RegCb.filter(F.action == "export"))
async def cb_analyze_export(
    callback: CallbackQuery, callback_data: RegCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    entity_id = callback_data.entity_id
    entity_type = callback_data.entity_type

    data = await _get_or_fetch_analysis(pool, callback.from_user.id, entity_id, entity_type)
    if not data:
        await callback.answer("❌ Нет данных для экспорта.", show_alert=True)
        return

    from services.entity_analyzer import format_export
    report = format_export(data)

    # Send as a document for easy copying
    import io
    title = data.get("title") or data.get("name") or str(entity_id)
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:30]
    buf = io.BytesIO(report.encode())
    buf.name = f"analysis_{safe_title}.txt"

    try:
        await callback.message.answer_document(
            buf,
            caption=f"📊 Полный отчёт: {html.escape(title)}",
            parse_mode="HTML",
        )
    except Exception as e:
        # Fallback: send as message chunks
        for i in range(0, len(report), 4000):
            await callback.message.answer(
                f"<code>{html.escape(report[i:i+4000])}</code>",
                parse_mode="HTML",
            )


async def _show_analysis_page(
    message,
    data: dict,
    entity_id: int,
    entity_type: str,
    page: int,
) -> None:
    from services.entity_analyzer import PAGE_FORMATTERS
    formatter = PAGE_FORMATTERS.get(page, PAGE_FORMATTERS[0])
    text = formatter(data)
    kb = _analyze_kb(entity_id, entity_type, page)
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
