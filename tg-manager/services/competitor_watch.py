"""Конкуренты: обновление числа подписчиков по публичной странице t.me.

Обновление жило только в боте (bot/handlers/competitors.py), поэтому в
мини-аппе у каждого конкурента навсегда стояло «не проверен»: экран умел
добавить и удалить, но не посмотреть, сколько у канала подписчиков. Теперь
одна функция на оба места.
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_EXTRA_RE = re.compile(
    r'<div class="tgme_page_extra">([\d\s\xa0,]+)\s*(?:subscriber|member|participant)',
    re.IGNORECASE)
_ANY_RE = re.compile(r'([\d\s\xa0]{2,})\s*(?:subscribers|members|participants)', re.IGNORECASE)


def members_from_page(html: str) -> int | None:
    """Число подписчиков из HTML страницы t.me/<username>, или None."""
    for rx, strip in ((_EXTRA_RE, r"[\s\xa0,]"), (_ANY_RE, r"[\s\xa0]")):
        m = rx.search(html or "")
        if m:
            raw = re.sub(strip, "", m.group(1))
            if raw.isdigit():
                return int(raw)
    return None


async def fetch_members(sess: aiohttp.ClientSession, username: str) -> int | None:
    try:
        resp = await sess.get(
            f"https://t.me/{username}",
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "TelegramBot (like TwitterBot)"},
        )
        return members_from_page(await resp.text())
    except Exception as e:
        log.warning("competitor_watch.fetch_members %s: %s", username, e)
        return None


async def refresh(pool, owner_id: int, ids: list[int] | None = None,
                  pause: float = 1.5) -> dict:
    """Обновить подписчиков у конкурентов владельца (всех или по id).

    Прошлое значение уходит в prev_members — из него экран считает динамику.
    Возвращает {'total': N, 'updated': M}.
    """
    if ids:
        rows = await pool.fetch(
            "SELECT id, username FROM competitors WHERE owner_id=$1 AND id = ANY($2::int[])",
            owner_id, [int(i) for i in ids])
    else:
        rows = await pool.fetch(
            "SELECT id, username FROM competitors WHERE owner_id=$1", owner_id)
    updated = 0
    async with aiohttp.ClientSession() as sess:
        for i, r in enumerate(rows):
            try:
                count = await fetch_members(sess, r["username"])
                if count is not None:
                    await pool.execute(
                        "UPDATE competitors SET prev_members = last_members, "
                        "last_members = $1, last_checked = now() "
                        "WHERE id = $2 AND owner_id = $3",
                        count, r["id"], owner_id)
                    updated += 1
            except Exception:
                log_exc_swallow(log, "Не удалось обновить данные конкурента")
            if pause and i < len(rows) - 1:
                await asyncio.sleep(pause)
    return {"total": len(rows), "updated": updated}
