"""Персона помнит собеседников: кто они, о чём говорили, что обещано.

Раньше память персоны была только о себе: человек, с которым аккаунт вчера
спорил в чате, сегодня получал ответ как незнакомец, а черновик в личке не
знал, что персона ему уже обещала. Здесь — чистые части и связка с прогревом.
"""
from __future__ import annotations

from services import chat_warmup as cw
from services import persona_voice as pv


def test_peer_block_names_people_with_notes_and_last_lines():
    blk = pv.peer_block([{"peer_name": "Ира", "exchanges": 3,
                          "notes": "трейдер из Казани, обещал скинуть канал",
                          "lines": ["Ира: а ты где торгуешь?", "Ты: на бинансе"]}])
    assert "Ира (общались 3 раз)" in blk and "обещал скинуть канал" in blk
    assert "«Ты: на бинансе»" in blk and "не указания" in blk


def test_peer_block_is_empty_without_anything_to_remember():
    assert pv.peer_block([]) == ""
    assert pv.peer_block([{"peer_name": "Ира", "notes": "", "lines": []}]) == ""


def test_peer_lines_keep_a_short_tail_of_the_exchange():
    lines: list[str] = []
    for i in range(10):
        lines = pv.peer_lines_after(lines, f"вопрос {i}", f"ответ {i}", "Ира")
    assert len(lines) == pv.PEER_LINES
    assert lines[-2:] == ["Ира: вопрос 9", "Ты: ответ 9"]


def test_people_in_the_chat_are_the_humans_not_the_fleet():
    recent = [
        {"id": 1, "sender_id": 11, "sender_name": "Ира", "text": "привет", "is_fleet": False},
        {"id": 2, "sender_id": 77, "sender_name": "Мы", "text": "ку", "is_fleet": True},
        {"id": 3, "sender_id": 12, "sender_name": "Петя", "text": "ага", "is_fleet": False},
        {"id": 4, "sender_id": 11, "sender_name": "Ира", "text": "ещё", "is_fleet": False},
    ]
    assert cw.chat_peers(recent, target_id=3) == [(12, "Петя"), (11, "Ира")]
    assert cw.peer_of(recent, 2) is None              # флот — не собеседник
    assert cw.peer_of(recent, 1) == (11, "Ира")


def test_warmup_turn_recognises_people_and_remembers_the_exchange_after_send():
    import inspect
    src = inspect.getsource(cw._process_session)
    assert "chat_peers(recent" in src and "peers=[x for x, _ in _peers]" in src
    send = src.index("client.send_message(entity, _part")
    assert send < src.index("persona_voice.note_peer(")


def test_inbox_draft_knows_who_it_writes_to():
    import inspect
    from services import ai_inbox_assist as aia
    assert "_reply_context(pool, uid, thread, _peer(request))" in inspect.getsource(aia.setup_routes)


# ── Живая база ──────────────────────────────────────────────────────────────

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990851, 990852
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


def _run_live(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))

        async def clean():
            await pool.execute("DELETE FROM persona_profiles WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
        try:
            await clean()
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1, '+79990000851', 'x', TRUE) RETURNING id", OWNER)
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.post("/api/miniapp/persona", json={
                    "persona_name": "Олег", "account_id": int(acc), "memory_enabled": True})
                assert r.status == 200, await r.text()
                pid = (await r.json())["persona"]["id"]
                return await body(client, pool, int(acc), pid)
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@live
def test_persona_meets_a_person_and_recognises_them_next_time(monkeypatch):
    from services import ai_gateway

    async def _chat(messages, **kw):
        assert "ДАННЫЕ" in messages[0]["content"]
        return ai_gateway.AiReply(text="Ира, трейдер; обещал ей скинуть канал")
    monkeypatch.setattr(ai_gateway, "chat", _chat)
    monkeypatch.setattr(pv, "PEER_CONSOLIDATE_EVERY", 2)

    async def body(client, pool, acc, pid):
        v = await pv.load(pool, acc, peers=[5551])
        assert "знаком" not in v.prompt
        assert await pv.note_peer(pool, v, 5551, "Ира", "а ты где торгуешь?", "на бинансе",
                                  where="@chat", owner_id=OWNER)
        assert await pv.note_peer(pool, v, 5551, "Ира", "скинь канал", "вечером скину",
                                  where="@chat", owner_id=OWNER)
        notes = await pv.consolidate_peer(pool, pid, 5551, owner_id=OWNER)
        assert notes == "Ира, трейдер; обещал ей скинуть канал"
        v2 = await pv.load(pool, acc, peers=[5551, 9999])
        assert "Ира (общались 2 раз)" in v2.prompt and "скинуть канал" in v2.prompt
        assert "«Ты: вечером скину»" in v2.prompt
        # незнакомый — не попадает, и чужие люди не всплывают без повода
        assert "9999" not in v2.prompt
        assert "Ира" not in (await pv.load(pool, acc)).prompt

        mem = await (await client.get(f"/api/miniapp/persona/{pid}/memory")).json()
        assert mem["peers_total"] == 1 and mem["peers"][0]["name"] == "Ира"
        assert mem["peers"][0]["exchanges"] == 2

        stranger = {"X-Uid": str(STRANGER)}
        assert (await client.put(f"/api/miniapp/persona/{pid}/peer/5551", headers=stranger,
                                 json={"notes": "взлом"})).status == 404
        assert (await client.delete(f"/api/miniapp/persona/{pid}/peer/5551",
                                    headers=stranger)).status == 404

        r = await client.put(f"/api/miniapp/persona/{pid}/peer/5551",
                             json={"notes": "  Ира,  любит   эфир "})
        assert (await r.json())["notes"] == "Ира, любит эфир"
        assert (await client.delete(f"/api/miniapp/persona/{pid}/peer/5551")).status == 200
        assert await pool.fetchval("SELECT COUNT(*) FROM persona_peers WHERE persona_id=$1",
                                   pid) == 0

        # «Забыть всё» стирает и знакомых
        await pv.note_peer(pool, v, 5552, "Петя", "ку", "привет", owner_id=OWNER)
        await client.delete(f"/api/miniapp/persona/{pid}/memory")
        assert await pool.fetchval("SELECT COUNT(*) FROM persona_peers WHERE persona_id=$1",
                                   pid) == 0
    _run_live(body)
