"""Сообщение админам платформы о поломке — одна дверь для фоновых сбоев.

ЗАЧЕМ (D27). Админ узнавал о поломке из лога, а не из сообщения:
`service_supervisor` молча перезапускал упавший фоновый сервис, а
`error_monitor` при всплеске ошибок в обработчиках бота писал только
`log.warning`. Лог никто не читает, пока клиент не пожалуется.

КАК. `alert_admins` берёт получателей тем же способом, что и остальные
платформенные алерты (`platform_alert_recipients`: `ADMIN_IDS` плюс живые
сессии админки), и держит персистентный анти-повтор на каждого получателя
(`notify_dedup_ok`) — сервис, падающий раз в 30 секунд, не заваливает админа
сотней сообщений, и окно переживает рестарт. Если доставка не удалась, отметка
снимается — следующий сбой будет сообщён.

`make_alerter(pool, bot)` замыкает пул и бота: присмотр и мидлварь получают
готовую функцию «(ключ, текст)», которая НИКОГДА не бросает и не висит дольше
`timeout` — сбой оповещения не должен ломать то, о чём оно сообщает.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Awaitable, Callable

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

DEFAULT_REPEAT_S = 3600

Alerter = Callable[[str, str], Awaitable[None]]


def esc(text: object, limit: int = 300) -> str:
    """Текст исключения в HTML-сообщение: обрезан и экранирован."""
    return html.escape(str(text)[:limit])


async def alert_admins(pool, bot, key: str, text: str,
                       repeat_s: int = DEFAULT_REPEAT_S) -> int:
    """Отправить `text` админам платформы. Возвращает, скольким ушло."""
    if bot is None or pool is None:
        return 0
    try:
        from bot.utils.subscription import platform_alert_recipients
        from database import db as _db

        admin_ids = await platform_alert_recipients(pool)
    except Exception:
        log_exc_swallow(log, "platform_alert: список админов недоступен")
        return 0
    if not admin_ids:
        log.critical("platform_alert: сообщить некому (ни ADMIN_IDS, ни живых "
                     "сессий админки): %s", key)
        return 0
    sent = 0
    for aid in admin_ids:
        try:
            if not await _db.notify_dedup_ok(pool, int(aid), key, repeat_s):
                continue
        except Exception:
            # Fail-open: сбой анти-повтора не повод молчать о поломке.
            log_exc_swallow(log, "platform_alert: анти-повтор недоступен")
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            log_exc_swallow(log, f"platform_alert: алерт админу {aid} не ушёл")
            try:
                await _db.notify_dedup_forget(pool, int(aid), key)
            except Exception:
                pass
    return sent


def make_alerter(pool, bot, *, repeat_s: int = DEFAULT_REPEAT_S,
                 timeout: float = 10.0) -> Alerter:
    """Функция «(ключ, текст)» для мест, где нет пула и бота под рукой."""

    async def _alert(key: str, text: str) -> None:
        try:
            await asyncio.wait_for(
                alert_admins(pool, bot, key, text, repeat_s), timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc_swallow(log, f"platform_alert: {key} не отправлен")

    return _alert


async def safe_alert(alert: Alerter | None, key: str, text: str) -> None:
    """Вызвать оповещение, если оно задано; любой его сбой — только в лог."""
    if alert is None:
        return
    try:
        await alert(key, text)
    except asyncio.CancelledError:
        raise
    except Exception:
        log_exc_swallow(log, f"platform_alert: {key} не отправлен")
