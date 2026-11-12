"""Голос персоны на настоящей базе: черты, память, экран памяти.

Запросы новые (schema_v341, persona_voice, ручки памяти), а проверка текста
запроса не проверяет, что Postgres его примет. Здесь:
* экран сохраняет и отдаёт все черты голоса, правка — тоже;
* сказанное пишется в память, голос следующего хода его видит;
* пересборка «фактов о себе» пишет факты и подрезает старую память;
* владелец видит, правит и стирает память — только своей персоны.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990841, 990842
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


async def _clean(pool):
    await pool.execute(
        "DELETE FROM persona_profiles WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig_uid = M._get_uid
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))
        try:
            await _clean(pool)
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1, '+79990000841', 'x', TRUE) RETURNING id", OWNER)
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool, int(acc))
            finally:
                await client.close()
        finally:
            M._get_uid = orig_uid
            try:
                await _clean(pool)
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


_FORM = {
    "persona_name": "Олег Смирнов", "speech_style": "casual", "tone": "skeptical",
    "age": 31, "gender": "m", "city": "Казань", "occupation": "трейдер",
    "emoji_level": "none", "literacy": "sloppy", "taboo_topics": "политика",
    "sample_messages": "ну хз\nкто пробовал?\n\n", "memory_enabled": True,
}


@live
def test_voice_traits_are_saved_listed_and_edited():
    async def body(client, pool, acc):
        r = await client.post("/api/miniapp/persona", json={**_FORM, "account_id": acc})
        assert r.status == 200, await r.text()
        pid = (await r.json())["persona"]["id"]
        lst = await (await client.get("/api/miniapp/personas")).json()
        p = next(x for x in lst["personas"] if x["id"] == pid)
        assert (p["gender"], p["city"], p["occupation"]) == ("m", "Казань", "трейдер")
        assert (p["emoji_level"], p["literacy"]) == ("none", "sloppy")
        assert p["sample_messages"] == ["ну хз", "кто пробовал?"]
        assert lst["genders"]["m"] == "мужской"

        r = await client.put(f"/api/miniapp/persona/{pid}",
                             json={**_FORM, "account_id": acc, "gender": "f",
                                   "emoji_level": "чепуха", "memory_enabled": False})
        assert r.status == 200, await r.text()
        row = await pool.fetchrow("SELECT * FROM persona_profiles WHERE id=$1", pid)
        assert row["gender"] == "f" and row["emoji_level"] == "rare"   # мусор → по умолчанию
        assert row["memory_enabled"] is False
    _run(body)


@live
def test_said_is_remembered_and_the_next_turn_sees_it(monkeypatch):
    from services import ai_gateway, persona_voice as pv

    async def _chat(messages, **kw):
        return ai_gateway.AiReply(text="живёт в Казани; торгует криптой")
    monkeypatch.setattr(ai_gateway, "chat", _chat)
    monkeypatch.setattr(pv, "MEMORY_KEEP", 3)

    async def body(client, pool, acc):
        r = await client.post("/api/miniapp/persona", json={**_FORM, "account_id": acc})
        pid = (await r.json())["persona"]["id"]
        v = await pv.load(pool, acc)
        assert v.persona and v.persona.persona_id == pid and v.recent == []
        for t in ("я из Казани", "торгую с утра", "крипта лучше акций", "кофе без сахара"):
            assert await pv.remember(pool, v, "chat", t, entity="@chat", owner_id=OWNER)
        v2 = await pv.load(pool, acc)
        assert v2.recent[-1] == "кофе без сахара" and "«я из Казани»" in v2.prompt

        facts = await pv.consolidate(pool, pid, owner_id=OWNER)
        assert facts == "живёт в Казани; торгует криптой"
        row = await pool.fetchrow(
            "SELECT self_facts, self_facts_at FROM persona_profiles WHERE id=$1", pid)
        assert row["self_facts"] == facts and row["self_facts_at"] is not None
        n = await pool.fetchval("SELECT COUNT(*) FROM persona_memory WHERE persona_id=$1", pid)
        assert n == 3, "старая память подрезается до потолка"
        v3 = await pv.load(pool, acc)
        assert "живёт в Казани; торгует криптой" in v3.prompt
        # после пересборки счётчик свежих сброшен — модель не зовётся на каждой фразе
        fresh = await pool.fetchval(
            "SELECT COUNT(*) FROM persona_memory m JOIN persona_profiles p ON p.id=m.persona_id "
            "WHERE m.persona_id=$1 AND m.created_at > COALESCE(p.self_facts_at, 'epoch'::timestamptz)",
            pid)
        assert fresh == 0
    _run(body)


@live
def test_owner_sees_edits_and_forgets_memory_strangers_do_not():
    from services import persona_voice as pv

    async def body(client, pool, acc):
        r = await client.post("/api/miniapp/persona", json={**_FORM, "account_id": acc})
        pid = (await r.json())["persona"]["id"]
        v = await pv.load(pool, acc)
        await pv.remember(pool, v, "comment", "под постом про биток", entity="@ch",
                          owner_id=OWNER)

        mem = await (await client.get(f"/api/miniapp/persona/{pid}/memory")).json()
        assert mem["total"] == 1 and mem["items"][0]["kind_label"] == "Комментарий"
        assert mem["items"][0]["where"] == "@ch"

        stranger = {"X-Uid": str(STRANGER)}
        assert (await client.get(f"/api/miniapp/persona/{pid}/memory",
                                 headers=stranger)).status == 404
        assert (await client.put(f"/api/miniapp/persona/{pid}/memory", headers=stranger,
                                 json={"self_facts": "взлом"})).status == 404
        assert (await client.delete(f"/api/miniapp/persona/{pid}/memory",
                                    headers=stranger)).status == 404
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM persona_memory WHERE persona_id=$1", pid) == 1

        r = await client.put(f"/api/miniapp/persona/{pid}/memory",
                             json={"self_facts": "  живёт   в Казани "})
        assert (await r.json())["self_facts"] == "живёт в Казани"
        d = await (await client.delete(f"/api/miniapp/persona/{pid}/memory")).json()
        assert d["deleted"] == 1
        mem = await (await client.get(f"/api/miniapp/persona/{pid}/memory")).json()
        assert mem["total"] == 0 and mem["self_facts"] == ""
    _run(body)


@live
def test_old_statement_on_the_topic_comes_back_from_the_base():
    from services import persona_voice as pv

    async def body(client, pool, acc):
        r = await client.post("/api/miniapp/persona", json={**_FORM, "account_id": acc})
        assert r.status == 200
        v = await pv.load(pool, acc)
        await pv.remember(pool, v, "chat", "я вообще из Казани", owner_id=OWNER)
        for i in range(pv.RECENT_IN_PROMPT + 2):
            await pv.remember(pool, v, "chat", f"про рынок номер {i}", owner_id=OWNER)
        plain = await pv.load(pool, acc)
        assert "из Казани" not in plain.prompt, "старая фраза не должна висеть всегда"
        topical = await pv.load(pool, acc, context="а ты из какого города? Казань?")
        assert "«я вообще из Казани»" in topical.prompt
    _run(body)
