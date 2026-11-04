"""Бан владельца платформы действительно закрывает доступ.

Админ «банил» пользователя (`platform_users.is_banned = true`), пользователь
получал «Ваш аккаунт был заблокирован», — и продолжал пользоваться всем: ни бот,
ни API мини-аппа этот флаг не читали. Забаненный за спам или мошенничество
владелец дальше запускал рассылки и управлял аккаунтами.

Проверка стоит на входе: middleware бота (личные сообщения, кнопки, инлайн) и
middleware API мини-аппа. Ответ кэшируется на короткое время, чтобы не ходить
в базу на каждый апдейт; бан и разбан сбрасывают кэш сразу (`forget`).
Постоянные админы (ADMIN_IDS) не банятся. Если база недоступна — пропускаем:
отказ в обслуживании всем из-за сбоя базы хуже, чем минута доступа забаненному.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

_TTL = 30.0
_MAX = 50_000
_cache: dict[int, tuple[bool, float]] = {}

BANNED_TEXT = ("🚫 Ваш аккаунт заблокирован администратором.\n"
               "Если вы считаете это ошибкой, обратитесь в поддержку.")


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def forget(uid: int | None = None) -> None:
    """Сбросить кэш (после бана/разбана) — для одного или для всех."""
    if uid is None:
        _cache.clear()
    else:
        _cache.pop(int(uid), None)


async def is_banned(pool, uid: int | None) -> bool:
    if not uid or pool is None or int(uid) in _admin_ids():
        return False
    uid = int(uid)
    now = time.monotonic()
    hit = _cache.get(uid)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    try:
        # Только настоящий true: число или строка из чужого ответа — не бан.
        val = (await pool.fetchval(
            "SELECT COALESCE(is_banned, false) FROM platform_users WHERE user_id=$1",
            uid)) is True
    except Exception:
        log.debug("platform_ban: проверка не удалась uid=%s", uid, exc_info=True)
        return False
    if len(_cache) >= _MAX:
        _cache.clear()
    _cache[uid] = (val, now)
    return val
