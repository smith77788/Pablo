"""Тихие часы на живом Postgres: очередь, сводка, чистка."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 88_200_001


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


async def _scenario(pool):
    from database import db
    from services import quiet_hours as qh
    await pool.execute("DELETE FROM notification_held WHERE user_id=$1", OWNER)
    await pool.execute("DELETE FROM notification_settings WHERE user_id=$1", OWNER)
    # Окно тишины — с текущего часа UTC на два часа вперёд: сейчас точно «ночь».
    h = dt.datetime.now(dt.timezone.utc).hour
    await qh.save(pool, OWNER, True, h, (h + 2) % 24, "UTC")
    await qh.save(pool, OWNER, True, h, (h + 2) % 24, "Mars/Olympus")   # чужой пояс не пишется
    assert await pool.fetchval(
        "SELECT tz FROM notification_settings WHERE user_id=$1", OWNER) == "UTC"
    db._notify_cooldown.clear()
    bot = _Bot()
    await db.notify_if_enabled(pool, bot, OWNER, "op_complete", "<b>Итог</b> операции")
    await db.notify_if_enabled(pool, bot, OWNER, "restriction", "Аккаунт ограничен")
    held = await pool.fetchval("SELECT COUNT(*) FROM notification_held WHERE user_id=$1", OWNER)
    assert bot.sent == [(OWNER, "Аккаунт ограничен")] and held == 1

    assert await qh.flush_once(pool, bot) == 0          # ещё тихо
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
    assert await qh.flush_once(pool, bot, later) == 1
    assert "Итог операции" in bot.sent[-1][1]
    assert await pool.fetchval(
        "SELECT COUNT(*) FROM notification_held WHERE user_id=$1", OWNER) == 0
    await pool.execute("DELETE FROM notification_settings WHERE user_id=$1", OWNER)
    db._notify_cooldown.clear()


def test_quiet_hours_round_trip():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await _scenario(pool)
        finally:
            await pool.close()
    asyncio.run(_go())
