"""Входы владельца — запросы на живой базе (schema_v380).

Повторный вход того же устройства продлевает строку, новое устройство даёт
тревогу (но не самый первый вход), завершение «все, кроме этого» не трогает
текущий сеанс, а завершение связанного устройства отзывает и его ключ.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import owner_sessions as osess

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995782
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")


def _meta(ua, country="UA", ip="198.51.100.7"):
    return {"ua": ua, "fp": osess.device_fp(ua), "label": osess.device_label(ua),
            "ip": ip, "country": country}


def test_open_list_and_revoke(monkeypatch):
    alerts = []

    def _spawn(coro, name=None):
        alerts.append(name)
        coro.close()

    import services.bg_tasks as bg
    monkeypatch.setattr(bg, "spawn", _spawn)

    async def _go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM owner_sessions WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM device_pairings WHERE owner_id=$1", OWNER)
            a = await osess.open_session(pool, OWNER, "telegram", _meta("Phone A"))
            assert a and alerts == [], "первый вход — не тревога"
            again = await osess.open_session(pool, OWNER, "telegram", _meta("Phone A", ip="10.0.0.1"))
            assert again == a, "повторный вход того же устройства плодит строки"
            b = await osess.open_session(pool, OWNER, "telegram", _meta("Laptop B", country="NL"))
            assert b and b != a and alerts == ["owner_login_alert"]

            pid = await pool.fetchval(
                "INSERT INTO device_pairings(owner_id, token_fp, paired_at) "
                "VALUES($1, 'fp-x', now()) RETURNING id", OWNER)
            c = await osess.open_session(pool, OWNER, "device", _meta("Browser C"),
                                         pairing_id=int(pid))
            rows = await osess.list_sessions(pool, OWNER)
            assert {r["sid"] for r in rows} == {a, b, c}

            n = await osess.revoke(pool, OWNER, all_except_sid=a)
            assert n == 2 and osess.is_revoked(b) and osess.is_revoked(c)
            assert not osess.is_revoked(a)
            assert await pool.fetchval(
                "SELECT revoked_at IS NOT NULL FROM device_pairings WHERE id=$1", pid), (
                "ключ связанного устройства пережил завершение сеанса")
            # Чужой владелец не завершит чужой вход.
            sid_a = await pool.fetchval("SELECT id FROM owner_sessions WHERE sid=$1", a)
            assert await osess.revoke(pool, OWNER + 1, session_id=int(sid_a)) == 0
            osess._REVOKED.clear()
            await osess.sync_revoked(pool)
            assert osess.is_revoked(b) and not osess.is_revoked(a)
        finally:
            await pool.execute("DELETE FROM owner_sessions WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM device_pairings WHERE owner_id=$1", OWNER)
            osess._REVOKED.clear()
            await pool.close()
    asyncio.new_event_loop().run_until_complete(_go())
