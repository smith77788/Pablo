"""«Хранилище» (Echo Vault): приём Telegram Business-апдейтов и зеркалирование
переписки в архив.

Подключение бота в настройках бизнес-аккаунта присылает business_connection;
далее приходят business_message / edited_business_message /
deleted_business_messages. Мы сохраняем контент (шифрованный), фиксируем правки и
помечаем удаления — НЕ стирая сам контент (в этом весь смысл хранилища).

Внешний контент из Telegram — ДАННЫЕ, не инструкции: ничего из него не
исполняем, только сохраняем; сырой текст/секреты не логируем.
"""
from __future__ import annotations

import logging

import asyncpg
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from services import vault_service as vault

log = logging.getLogger(__name__)
router = Router(name="business_vault")


@router.message(Command("vault"))
async def cmd_vault(message: Message, pool: asyncpg.Pool) -> None:
    """Экран «Хранилище»: статус подключения, инструкция и кнопка открытия архива."""
    uid = message.from_user.id if message.from_user else None
    conn = await vault.active_connection_for_owner(pool, uid) if uid else None

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    _has_btn = False
    try:
        from config import MINI_APP_URL as _URL
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from aiogram.types import WebAppInfo
        url = _valid_mini_app_url(_URL)
        if url:
            # web_app-кнопка не передаёт start_param → кладём #vault в URL, мини-апп
            # его читает при загрузке и открывает Хранилище сразу.
            deep = f"{url}#vault" if "?" not in url else f"{url}&screen=vault"
            kb.button(text="🗄 Открыть Хранилище", web_app=WebAppInfo(url=deep))
            _has_btn = True
    except Exception:
        log.debug("cmd_vault: mini-app URL недоступен")

    me = await message.bot.get_me()
    handle = f"@{me.username}" if me and me.username else "этого бота"
    if conn:
        head = ("✅ <b>Хранилище подключено.</b>\n\n"
                "Бот сохраняет все сообщения в ваших личных чатах — даже удалённые. "
                "Откройте архив кнопкой ниже.")
        if not conn.get("can_reply"):
            head += ("\n\nЧтобы отвечать собеседникам прямо из «Хранилища», включите боту "
                     "право отвечать в настройках бизнес-аккаунта.")
    else:
        head = (
            "🗄 <b>Хранилище (Echo Vault)</b>\n\n"
            "Бот <b>автоматически сохраняет все сообщения</b>, которые вы отправляете и "
            "получаете в своих чатах — чтобы вы могли вернуться к ним в любой момент, "
            "<b>даже если диалог удалён</b>.\n\n"
            "Чтобы бот начал запись, подключите его в бизнес-аккаунте:\n"
            "1. Настройки Telegram → <b>Telegram для бизнеса</b>\n"
            "2. <b>Чат-боты</b>\n"
            f"3. Введите <b>{handle}</b> и разрешите доступ к чатам\n\n"
            "🔒 Все сообщения хранятся в зашифрованном виде.")
    await message.answer(head, parse_mode="HTML",
                         reply_markup=kb.as_markup() if _has_btn else None)


def _rights_dict(bc: BusinessConnection) -> dict:
    rights = getattr(bc, "rights", None)
    if rights is None:
        return {}
    try:
        return rights.model_dump(exclude_none=True)  # pydantic (aiogram 3.x)
    except Exception:
        return {}


def _can_reply(bc: BusinessConnection) -> bool:
    # Bot API 9.0 перенёс can_reply в rights.can_reply; старое поле can_reply
    # оставлено для совместимости. Читаем оба, приоритет — rights.
    rights = getattr(bc, "rights", None)
    if rights is not None and getattr(rights, "can_reply", None) is not None:
        return bool(rights.can_reply)
    return bool(getattr(bc, "can_reply", False))


@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot,
                                 pool: asyncpg.Pool) -> None:
    """Подключение/отключение/изменение прав бизнес-бота."""
    owner = getattr(event, "user", None)
    owner_id = getattr(owner, "id", None)
    if owner_id is None:
        return
    is_enabled = bool(getattr(event, "is_enabled", True))
    can_reply = _can_reply(event)
    try:
        await vault.upsert_connection(
            pool, event.id, owner_id, getattr(event, "user_chat_id", None),
            can_reply, is_enabled, _rights_dict(event))
    except Exception:
        log.exception("vault: upsert_connection failed conn=%s", event.id)
        return
    log.info("vault: business connection %s owner=%s enabled=%s reply=%s",
             event.id, owner_id, is_enabled, can_reply)

    chat_id = getattr(event, "user_chat_id", None) or owner_id
    try:
        if is_enabled:
            await bot.send_message(
                chat_id,
                "✅ <b>Хранилище подключено!</b>\n\n"
                "Теперь бот сохраняет все сообщения в ваших личных чатах — вы сможете "
                "вернуться к ним в любой момент, <b>даже если диалог удалён</b>.\n\n"
                + ("Также вы можете <b>отвечать собеседникам прямо из «Хранилища»</b>.\n\n"
                   if can_reply else
                   "Чтобы отвечать собеседникам прямо из «Хранилища», включите боту "
                   "право отвечать в настройках бизнес-аккаунта.\n\n")
                + "Откройте <b>«Хранилище»</b>, чтобы просмотреть архив.",
                parse_mode="HTML")
        else:
            await bot.send_message(
                chat_id,
                "⏸ <b>Хранилище отключено.</b> Новые сообщения больше не сохраняются. "
                "Уже сохранённое остаётся в архиве.",
                parse_mode="HTML")
    except Exception:
        log.debug("vault: не удалось отправить уведомление о подключении owner=%s", owner_id)


async def _owner_for(pool: asyncpg.Pool, conn_id: str | None) -> int | None:
    if not conn_id:
        return None
    conn = await vault.get_connection(pool, conn_id)
    return conn["owner_id"] if conn else None


@router.business_message()
async def on_business_message(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    """Новое сообщение в бизнес-чате пользователя — архивируем + сенсор намерений."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        log.debug("vault: business_message без известного подключения %s", conn_id)
        return
    try:
        await vault.archive_message(pool, message, owner_id, conn_id)
    except Exception:
        log.exception("vault: archive_message failed owner=%s", owner_id)
    # Сенсор намерений: только ВХОДЯЩИЕ (что написал собеседник). Fail-open —
    # сбой сенсора не должен ломать архивацию.
    try:
        if vault.direction_of(message, owner_id) == "in":
            from services import intent_sensor
            await intent_sensor.scan_incoming(
                pool, bot, owner_id, vault.peer_of(message), vault.text_of(message))
    except Exception:
        log.debug("vault: intent_sensor failed owner=%s", owner_id)


@router.edited_business_message()
async def on_edited_business_message(message: Message, bot: Bot,
                                     pool: asyncpg.Pool) -> None:
    """Правка сообщения — сохраняем и старую, и новую версию; «ловец» шлёт
    было→стало (только для входящих правок собеседника)."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        return
    try:
        info = await vault.record_edit(pool, message, owner_id)
    except Exception:
        log.exception("vault: record_edit failed owner=%s", owner_id)
        return
    if not info.get("changed") or info.get("direction") != "in":
        return
    try:
        prefs = await vault.get_notify_prefs(pool, owner_id)
        if not prefs["notify_edited"]:
            return
        who = _esc(info.get("peer_name") or "Собеседник")
        await bot.send_message(
            prefs["user_chat_id"] or owner_id,
            f"✏️ <b>{who}</b> изменил сообщение:\n\n"
            f"<b>Было:</b> {_esc(_short(info['old']))}\n"
            f"<b>Стало:</b> {_esc(_short(info['new']))}",
            parse_mode="HTML")
    except Exception:
        log.debug("vault: edit-уведомление не отправлено owner=%s", owner_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot,
                                       pool: asyncpg.Pool) -> None:
    """Удаление у пользователя — помечаем в архиве, контент СОХРАНЯЕМ; «ловец»
    шлёт, что именно удалил собеседник."""
    owner_id = await _owner_for(pool, getattr(event, "business_connection_id", None))
    if owner_id is None:
        return
    chat_id = getattr(getattr(event, "chat", None), "id", None)
    msg_ids = list(getattr(event, "message_ids", None) or [])
    if chat_id is None or not msg_ids:
        return
    try:
        # Сначала достаём содержимое ВХОДЯЩИХ (для уведомления), пока не потеряли контекст.
        incoming = await vault.deleted_incoming_rows(pool, owner_id, chat_id, msg_ids)
        n = await vault.mark_deleted(pool, owner_id, chat_id, msg_ids)
        log.info("vault: помечено удалёнными %d сообщ. owner=%s chat=%s", n, owner_id, chat_id)
    except Exception:
        log.exception("vault: mark_deleted failed owner=%s", owner_id)
        return
    if not incoming:
        return  # удалены только исходящие самого пользователя — не сигнал
    try:
        prefs = await vault.get_notify_prefs(pool, owner_id)
        if not prefs["notify_deleted"]:
            return
        who = _esc((incoming[0].get("peer_name")) or "Собеседник")
        lines = [f"🗑 <b>{who}</b> удалил сообщений: {len(incoming)}"]
        has_media = False
        for m in incoming[:5]:
            body = m["text"] or m["media_label"] or "—"
            if m.get("media_label"):
                has_media = True
            lines.append(f"• {_esc(_short(body))}")
        if has_media:
            # Подпись «📷 Фото» без самого файла бесполезна ровно в тот момент,
            # когда он нужнее всего — вложение сохранено, и его можно открыть.
            lines.append("\n📎 Вложение сохранено — откройте, чтобы посмотреть.")
        await bot.send_message(prefs["user_chat_id"] or owner_id,
                               "\n".join(lines), parse_mode="HTML",
                               reply_markup=_open_chat_kb(chat_id))
    except Exception:
        log.debug("vault: delete-уведомление не отправлено owner=%s", owner_id)


def _open_chat_kb(chat_id: int):
    """Кнопка «открыть этот чат в Хранилище».

    Без неё уведомление было тупиком: «собеседник удалил сообщение» — а чтобы
    увидеть ЧТО (особенно вложение, которое теперь можно открыть), надо было
    вручную зайти в приложение, найти раздел и нужный чат среди прочих.
    Мини-апп понимает «#vault:<chat_id>» и открывает сразу переписку.
    Возвращает None, если URL мини-аппа не настроен — тогда шлём как раньше.
    """
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL

        url = _valid_mini_app_url(MINI_APP_URL)
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Открыть переписку",
                                 web_app=WebAppInfo(url=f"{url}#vault:{int(chat_id)}"))
        ]])
    except Exception:
        return None


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


def _short(s, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return (s[:n] + "…") if len(s) > n else (s or "—")
