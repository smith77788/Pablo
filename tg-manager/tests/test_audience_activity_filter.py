"""Отбор аудитории не берёт в работу давно неактивных контактов.

Жалоба владельца: «не видно, когда контакт был в сети; модули, которые
используют контакты для работы, берут в работу тех, кто был в сети „давно“,
хотя это значит, что аккаунт неактивен».

Статус пишет парсер (account_manager._classify_last_seen): online/recently/
last_week/last_month — активные; 'offline' с точным временем старше 30 дней —
заходил давно. Рабочий срез (resolve_segment: рассылки/инвайты) давних не
берёт; просмотр списка (get_contacts) показывает всех. Нет данных (NULL/
unknown) — не отсекаем (fail-open).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services.contacts_hub import repository as repo


# ── Unit: WHERE рабочего среза фильтрует активность, просмотр — нет ────────────

def test_segment_where_filters_inactive_by_default():
    where, _, _ = repo._segment_where(7, {})
    assert "last_seen_type = 'offline'" in where and "30 days" in where, (
        "рабочий срез не фильтрует давно-оффлайн контактов")


def test_segment_where_include_inactive_skips_filter():
    where, _, _ = repo._segment_where(7, {"include_inactive": True})
    assert "30 days" not in where, "include_inactive должен снимать фильтр активности"


# ── E2e: реальная выборка на Postgres ─────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
e2e = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


@e2e
def test_operations_skip_long_inactive_but_view_shows_all():
    import asyncpg

    async def scenario():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        uid = 888042
        rows = [
            ("online", None, "a_online"),
            ("last_month", None, "a_month"),
            ("offline", "now() - interval '1 day'", "a_off_recent"),
            ("offline", "now() - interval '60 days'", "a_off_old"),  # отсечь
            ("unknown", None, "a_unknown"),
            (None, None, "a_null"),
        ]
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid)
            for i, (t, at, un) in enumerate(rows):
                await pool.execute(
                    "INSERT INTO unified_contacts(owner_id,telegram_user_id,username,"
                    f"last_seen_type,last_seen_at,excluded) VALUES($1,$2,$3,$4,{at or 'NULL'},FALSE)",
                    uid, 2000 + i, un, t)

            seg = {r["username"] for r in await repo.resolve_segment(pool, uid, {})}
            assert "a_off_old" not in seg, "давно-оффлайн попал в рабочий срез"
            assert {"a_online", "a_month", "a_off_recent"} <= seg, "активные потерялись"
            assert {"a_unknown", "a_null"} <= seg, "контакты без данных выброшены"

            full = await repo.get_contacts(pool, uid, limit=100)
            fnames = {c["username"] for c in full["contacts"]}
            assert "a_off_old" in fnames, "просмотр скрыл неактивного"
            assert full["total"] == len(rows)
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
