"""Текст исключения — в сообщение владельцу только через эту дверь.

В мини-аппе текст ошибки уходит наружу одной дверью (`mini_app_api._err`):
она вычищает секреты и подменяет английскую фразу русской. В боте такой двери
не было, и три десятка мест писали `f"❌ Ошибка: {e}"` напрямую. Владелец по
английски не читает, а текст исключения — это `str()` Telethon/asyncpg:
английская фраза, иногда строка подключения или токен. Здесь то же самое
правило, что и в мини-аппе, плюс экранирование под parse_mode=HTML бота
(в тексте исключения бывают `<` и `>`, и Telegram отвергает такое сообщение
целиком — владелец не получает даже «Ошибка»).
"""
from __future__ import annotations

import html
import logging

log = logging.getLogger(__name__)

_LIMIT = 300
_ALERT_LIMIT = 150


def owner_error(exc: object, *, html_escape: bool = True) -> str:
    """Безопасная русская формулировка ошибки для сообщения в боте.

    `html_escape=False` — для всплывающего ответа на кнопку
    (`callback.answer`): его Telegram показывает как есть, без разметки.
    """
    raw = str(exc or "").strip()
    if not raw:
        return "неизвестная ошибка — подробности в журнале"
    try:
        from services.secret_masking import redact_secrets
        raw = redact_secrets(raw)
    except Exception:
        log.debug("owner_error: маскировка недоступна", exc_info=True)
        return "внутренняя ошибка — подробности в журнале"
    try:
        from services.mini_app_api import _owner_ru
        raw = _owner_ru(raw)
    except Exception:
        log.debug("owner_error: перевод недоступен", exc_info=True)
    # Всплывающий ответ на кнопку Telegram режет на 200 знаках и отвергает
    # длиннее целиком, а перед текстом ошибки обычно стоит своя подпись.
    limit = _LIMIT if html_escape else _ALERT_LIMIT
    if len(raw) > limit:
        raw = raw[:limit - 1] + "…"
    return html.escape(raw, quote=False) if html_escape else raw
