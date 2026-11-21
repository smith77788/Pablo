"""Короткие ссылки: правка, поиск и порядок «по переходам».

Раньше ошибку в адресе ссылки лечили только удалением — и ссылка, уже
розданная людям, переставала работать. Найти нужную среди сотни можно было
только листанием, а самые кликабельные — никак.

Запросы новые, поэтому проверка идёт на настоящей базе.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990791, 990792


def test_screen_has_edit_search_and_sort():
    ui = miniapp_source()
    assert "function lnkEdit(" in ui and "onclick=\"lnkEdit(" in ui
    assert 'id="lnkSearch"' in ui and 'id="lnkSort"' in ui
    assert "/api/miniapp/links/update" in ui


def _run(body):
    async def go():
        import asyncpg
        from services import link_shortener as L
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await L.ensure_table(pool)
            await pool.execute("DELETE FROM short_links WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            return await body(L, pool)
        finally:
            try:
                await pool.execute("DELETE FROM short_links WHERE owner_id = ANY($1::bigint[])",
                                   [OWNER, STRANGER])
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_update_keeps_code_and_clicks_and_is_owner_scoped():
    async def body(L, pool):
        r = await L.create(pool, OWNER, "https://old.example.org/a", "Старое",
                           custom_code="tstedit1", expires_at="2999-01-01T00:00:00Z")
        assert r["ok"], r
        await pool.execute("UPDATE short_links SET clicks=5 WHERE code='tstedit1'")
        res = await L.update(pool, OWNER, "tstedit1", target_url="https://new.example.org/b",
                             title="Новое", tags="промо", expires_at=None)
        assert res["ok"], res
        row = await pool.fetchrow("SELECT * FROM short_links WHERE code='tstedit1'")
        assert row["target_url"] == "https://new.example.org/b"
        assert row["title"] == "Новое" and row["clicks"] == 5
        assert row["expires_at"] is None, "пустой срок обязан снимать ограничение"
        bad = await L.update(pool, STRANGER, "tstedit1", target_url="https://evil.example.org")
        assert not bad["ok"] and bad.get("not_found")
        assert await pool.fetchval(
            "SELECT target_url FROM short_links WHERE code='tstedit1'") == "https://new.example.org/b"
        bad = await L.update(pool, OWNER, "tstedit1", target_url="")
        assert not bad["ok"]
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_search_and_sort_by_clicks():
    async def body(L, pool):
        for i, (title, clicks) in enumerate((("Акция 50%", 1), ("Новость", 9), ("Обычная", 3))):
            r = await L.create(pool, OWNER, f"https://s{i}.example.org/", title,
                               custom_code=f"tstsrch{i}")
            assert r["ok"], r
            await pool.execute("UPDATE short_links SET clicks=$1 WHERE code=$2",
                               clicks, f"tstsrch{i}")
        await L.create(pool, STRANGER, "https://x.example.org/", "Акция чужая",
                       custom_code="tstsrchx")
        got = await L.list_for_owner(pool, OWNER, q="акция")
        assert [x["code"] for x in got] == ["tstsrch0"]
        assert await L.count_for_owner(pool, OWNER, q="акция") == 1
        assert await L.count_for_owner(pool, OWNER, q="50%") == 1
        assert await L.count_for_owner(pool, OWNER, q="%") == 1, "% — символ, а не шаблон"
        assert await L.count_for_owner(pool, OWNER) == 3
        got = await L.list_for_owner(pool, OWNER, sort="clicks")
        assert [x["clicks"] for x in got] == [9, 3, 1]
    _run(body)
