"""Платформенные тревоги: владелец узнаёт о поломке из сообщения, а не из лога.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Два главных детектора поломок — всплеск ошибок в
хендлерах (`error_monitor`) и падение фонового сервиса (`service_supervisor`) —
до D27 писали только в лог. Лог читают после жалобы пользователя, то есть
поздно. У этих детекторов нет под рукой ни пула, ни бота, поэтому процесс
один раз привязывает их сюда (`bind`), а детекторы зовут `alert`.

ПРАВИЛА.
* Получатели — `platform_alert_recipients`, тот же круг, что у алертов оплаты
  и защитных записей: одно место решает, кто админ.
* Анти-повтор персистентный (`db.notify_dedup_ok`): падающий каждые 30 с
  сервис не заваливает админа и после рестарта процесса не шлёт то же заново.
* Текст ошибки проходит `redact_secrets`: строки сессий, токены и пароли
  прокси в сообщение не попадают.
* Тревога никогда не роняет того, кто её поднял: любой сбой здесь — лог.
"""

from __future__ import annotations

import asyncio
import html
import logging

from services.secret_masking import redact_secrets

log = logging.getLogger(__name__)

# Повтор одной и той же тревоги — не чаще раза в час.
DEFAULT_REPEAT_S = 3600

_ctx: dict = {"pool": None, "bot": None}
# Держим ссылки на фоновые задачи отправки, иначе сборщик мусора может
# снять их до завершения (asyncio хранит на задачи только слабые ссылки).
_pending: set[asyncio.Task] = set()


def bind(pool, bot) -> None:
    """Привязать пул и бота. Зовётся один раз при старте процесса."""
    _ctx["pool"] = pool
    _ctx["bot"] = bot


def is_bound() -> bool:
    return _ctx["pool"] is not None and _ctx["bot"] is not None


def clean(text: object, limit: int = 300) -> str:
    """Текст ошибки, безопасный для HTML-сообщения: без секретов и разметки."""
    return html.escape(redact_secrets(str(text), limit=limit))


async def alert(key: str, text: str, repeat_s: int = DEFAULT_REPEAT_S) -> int:
    """Отправить тревогу админам платформы. Возвращает число доставок.

    `key` — смысл тревоги («svc_crash:proxy_watchdog»): по нему работает
    анти-повтор. Не привязано или некому слать — 0 и запись в лог.
    """
    pool, bot = _ctx["pool"], _ctx["bot"]
    if pool is None or bot is None:
        log.warning("platform_alerts: не привязано, тревога только в лог: %s", key)
        return 0
    try:
        from bot.utils.subscription import platform_alert_recipients
        from database import db as _db

        admin_ids = await platform_alert_recipients(pool)
    except Exception:
        log.exception("platform_alerts: список админов недоступен (%s)", key)
        return 0
    if not admin_ids:
        log.critical("platform_alerts: сообщить некому (%s)", key)
        return 0
    sent = 0
    for aid in admin_ids:
        try:
            if not await _db.notify_dedup_ok(pool, int(aid), key, repeat_s):
                continue
        except Exception:
            log.exception("platform_alerts: анти-повтор недоступен")
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            log.exception("platform_alerts: тревога админу %s не ушла (%s)", aid, key)
            # Не доставили — снимаем отметку, чтобы следующая попытка прошла.
            try:
                await _db.notify_dedup_forget(pool, int(aid), key)
            except Exception:
                pass
    return sent


def alert_soon(key: str, text: str, repeat_s: int = DEFAULT_REPEAT_S) -> None:
    """Поставить тревогу в фон, не дожидаясь отправки (для горячих путей)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("platform_alerts: нет цикла событий, тревога только в лог: %s", key)
        return
    task = loop.create_task(alert(key, text, repeat_s))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
