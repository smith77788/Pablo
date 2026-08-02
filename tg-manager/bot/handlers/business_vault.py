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
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from services import vault_service as vault

log = logging.getLogger(__name__)
router = Router(name="business_vault")


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
async def on_business_message(message: Message, pool: asyncpg.Pool) -> None:
    """Новое сообщение в бизнес-чате пользователя — архивируем."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        log.debug("vault: business_message без известного подключения %s", conn_id)
        return
    try:
        await vault.archive_message(pool, message, owner_id, conn_id)
    except Exception:
        log.exception("vault: archive_message failed owner=%s", owner_id)


@router.edited_business_message()
async def on_edited_business_message(message: Message, pool: asyncpg.Pool) -> None:
    """Правка сообщения — сохраняем и старую, и новую версию."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        return
    try:
        await vault.record_edit(pool, message, owner_id)
    except Exception:
        log.exception("vault: record_edit failed owner=%s", owner_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted,
                                       pool: asyncpg.Pool) -> None:
    """Удаление у пользователя — помечаем в архиве, контент СОХРАНЯЕМ."""
    owner_id = await _owner_for(pool, getattr(event, "business_connection_id", None))
    if owner_id is None:
        return
    chat_id = getattr(getattr(event, "chat", None), "id", None)
    msg_ids = list(getattr(event, "message_ids", None) or [])
    if chat_id is None or not msg_ids:
        return
    try:
        n = await vault.mark_deleted(pool, owner_id, chat_id, msg_ids)
        log.info("vault: помечено удалёнными %d сообщ. owner=%s chat=%s", n, owner_id, chat_id)
    except Exception:
        log.exception("vault: mark_deleted failed owner=%s", owner_id)
