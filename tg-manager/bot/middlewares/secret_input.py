"""Сообщения с секретами удаляются из чата после обработки.

Бот просит прислать ему пароль 2FA, код входа, строку сессии, файл .session,
токены ботов, ключ ИИ, пароль почты. Прочитав, обработчики оставляли
сообщение в чате — а история Telegram хранится вечно, синхронизируется на все
устройства и видна любому, кто взял телефон в руки. Строка сессии или пароль
2FA в истории — это полный доступ к аккаунту.

Удаляем после обработчика (ему сообщение ещё нужно) и только в личке: в этих
состояниях бот ждёт секрет, и ничего другого человек сюда не пишет.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

log = logging.getLogger(__name__)

# Состояния FSM, в которых бот ждёт секрет. Имена — «Группа:состояние», как их
# отдаёт FSMContext.get_state(); тест сверяет их с настоящими классами.
SECRET_STATES = frozenset({
    "AccountLogin:waiting_code",
    "AccountLogin:waiting_2fa",
    "QrLogin2FA:waiting_password",
    "SessionImport:waiting_string_session",
    "SessionImport:waiting_pyrogram_json",
    "SessionImport:waiting_tdata_zip",
    "SessionImport:waiting_session_file",
    "SessionImport:waiting_batch_sessions",
    "AddBot:waiting_token",
    "UpdateToken:waiting_token",
    "ImportBots:waiting_tokens",
    "BotTokenImportFSM:waiting_tokens",
    "BotValidateFSM:waiting_tokens",
    "AiKeyFSM:waiting_key",
    "StrikeEmailFSM:awaiting_password",
})


class SecretInputMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        secret = False
        if isinstance(event, Message) and getattr(event.chat, "type", None) == "private":
            fsm = data.get("state")
            if fsm is not None:
                try:
                    secret = (await fsm.get_state()) in SECRET_STATES
                except Exception:
                    secret = False
        try:
            return await handler(event, data)
        finally:
            if secret:
                try:
                    await event.delete()
                except Exception:
                    # Уже удалено обработчиком или старше 48 ч — не страшно.
                    log.debug("secret_input: сообщение не удалено", exc_info=True)
