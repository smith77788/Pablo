"""Экран напоминаний не тащит тысячи просроченных строк — и говорит, сколько их.

У владельца, импортировавшего базу с датами, просроченных напоминаний тысячи.
`/uch/reminders` отдавал их ВСЕ: на стенде из 5000 — 2,7 МБ ответа, а экран
рисовал по три кнопки на каждую строку. Теперь под потолком самые старые, а
`overdue_total` — настоящий итог, и экран подписывает «Показаны N из M».

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_622
LATE = 230          # больше потолка
SOON = 3


def _go(body):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await _clean(p)
            await _seed(p)
            await body(p)
        finally:
            await _clean(p)
            await p.close()

    asyncio.run(_main())


async def _clean(p):
    await p.execute("DELETE FROM contact_crm WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)


async def _seed(p):
    # Просроченные: i-й просрочен на (LATE - i) часов, то есть самый старый — первый.
    await p.execute(
        """WITH c AS (
               INSERT INTO unified_contacts (owner_id, first_name)
               SELECT $1, 'late' || g FROM generate_series(1, $2) g
               RETURNING id, first_name)
           INSERT INTO contact_crm (owner_id, contact_id, next_reminder_at, next_reminder_text)
           SELECT $1, id, now() - make_interval(hours => $2 + 1 - substr(first_name, 5)::int),
                  'позвонить'
             FROM c""", OWNER, LATE)
    await p.execute(
        """WITH c AS (
               INSERT INTO unified_contacts (owner_id, first_name)
               SELECT $1, 'soon' || g FROM generate_series(1, $2) g RETURNING id)
           INSERT INTO contact_crm (owner_id, contact_id, next_reminder_at)
           SELECT $1, id, now() + interval '1 day' FROM c""", OWNER, SOON)


def test_screen_gets_the_oldest_under_the_cap_and_the_real_total():
    from services import mini_app_api as M
    from services import mini_app_uch as U

    async def body(pool):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        try:
            app = web.Application()
            U.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/api/miniapp/uch/reminders")
                assert r.status == 200
                d = await r.json()
            finally:
                await client.close()
        finally:
            M._get_uid = orig
        assert len(d["overdue"]) == U._OVERDUE_CAP < LATE
        assert d["overdue_total"] == LATE
        assert d["overdue"][0]["contact_name"] == "late1"     # самый старый
        assert len(d["upcoming"]) == SOON

    _go(body)


def test_engine_count_matches_full_list_and_assistant_counts_all():
    from services.contacts_hub import crm_engine as E

    async def body(pool):
        assert await E.count_crm_overdue(pool, OWNER) == LATE
        assert len(await E.get_crm_overdue(pool, OWNER)) == LATE   # без потолка — всё
        assert len(await E.get_crm_overdue(pool, OWNER, limit=5)) == 5

    _go(body)


def test_screen_says_the_list_is_cut():
    from tests.miniapp_source import js_function
    body = js_function("openUchReminders")
    assert "overdue_total" in body and "anCap(overdue.length" in body
