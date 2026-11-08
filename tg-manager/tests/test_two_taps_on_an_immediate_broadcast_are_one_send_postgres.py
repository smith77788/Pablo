"""Два тапа «Отправить сейчас» — одна рассылка, а не две.

Немедленная рассылка создаётся в два шага: строка в `broadcasts`, потом
операция с её id в params. У каждого тапа строка своя, значит и params свои, и
окно идемпотентности шины не срабатывало НИ РАЗУ. Запрет живого двойника в
исполнителе видит только рассылку в статусе 'running': на маленькой аудитории
первая успевала закончиться раньше, чем начиналась вторая, и подписчики
получали сообщение дважды. Повтор клиента после таймаута (деплой, холодный
старт) — тот же второй тап.

Теперь id строки не входит в личность run_broadcast (`dedup_ignore_keys`),
второй тап получает операцию первого, а свою строку снимает
(`broadcaster.adopt_submitted_broadcast`) — иначе её поднял бы старт процесса.
Разные тексты и разные сегменты по-прежнему две рассылки.

Нужен живой Postgres: дедуп шины — это SQL по JSONB и замок в базе.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993719
BOT = 993719001
TOKEN = "993719001:AAHtwotapsfixture_0123456789abcdefghij"
SUBS = [9937190 + i for i in range(3)]

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT)
    await p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=8)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture
def client(pool, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_broadcast, operation_bus

    async def _no_plan_gate(*a, **k):
        return False

    async def _no_immunity(*a, **k):
        return None

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: OWNER)
    monkeypatch.setattr(operation_bus, "_enforce_min_plan", _no_plan_gate)
    monkeypatch.setattr(operation_bus, "_enforce_immunity", _no_immunity)

    _run(_cleanup(pool))
    _run(pool.execute("INSERT INTO managed_bots(token, bot_id, username, added_by) "
                      "VALUES($1,$2,'two_taps_fixture',$3)", TOKEN, BOT, OWNER))
    _run(pool.executemany(
        "INSERT INTO bot_users(bot_id, user_id, language_code, is_active, is_blocked, suspect) "
        "VALUES($1,$2,'ru',TRUE,FALSE,FALSE)", [(BOT, u) for u in SUBS]))

    app = web.Application()
    mini_app_broadcast.setup_routes(app, pool)

    async def _mk():
        c = TestClient(TestServer(app))
        await c.start_server()
        return c

    c = _run(_mk())
    yield c
    _run(c.close())


def _send(client, text="Скидка до вечера", segment="all"):
    async def _go():
        resp = await client.post("/api/miniapp/broadcast", json={
            "bot_id": BOT, "text": text, "segment": segment})
        return resp.status, await resp.json()

    return _go()


def _ops(pool) -> int:
    return int(_run(pool.fetchval(
        "SELECT count(*) FROM operation_queue WHERE owner_id=$1 "
        "AND op_type='run_broadcast'", OWNER)))


def _pending(pool) -> list[int]:
    return [int(r["id"]) for r in _run(pool.fetch(
        "SELECT id FROM broadcasts WHERE bot_id=$1 AND status='pending' ORDER BY id",
        BOT))]


def test_two_simultaneous_taps_are_one_broadcast(pool, client):
    async def _both():
        return await asyncio.gather(_send(client), _send(client))

    (s1, b1), (s2, b2) = _run(_both())
    assert (s1, s2) == (200, 200), (b1, b2)
    assert _ops(pool) == 1, (
        "два тапа поставили две рассылки — подписчики получат сообщение дважды")
    assert b1["op_id"] == b2["op_id"]
    assert b1["broadcast_id"] == b2["broadcast_id"], (
        "второй тап показывает владельцу рассылку, которую никто не отправит")
    assert _pending(pool) == [b1["broadcast_id"]], (
        "лишняя строка второго тапа осталась 'pending' — старт процесса "
        "отправил бы её всей аудитории мимо операции")


def test_a_retry_after_the_first_finished_is_still_one_send(pool, client):
    """Маленькая аудитория: первая рассылка уже закончилась, второй тап — нет."""
    s1, b1 = _run(_send(client))
    _run(pool.execute("UPDATE operation_queue SET status='done', finished_at=now() "
                      "WHERE id=$1", b1["op_id"]))
    _run(pool.execute("UPDATE broadcasts SET status='completed' WHERE id=$1",
                      b1["broadcast_id"]))
    s2, b2 = _run(_send(client))
    assert (s1, s2) == (200, 200)
    assert b2["op_id"] == b1["op_id"] and _ops(pool) == 1
    assert _pending(pool) == []


def test_different_text_or_segment_is_a_different_broadcast(pool, client):
    _, a = _run(_send(client, text="Первая"))
    _, b = _run(_send(client, text="Вторая"))
    _, c = _run(_send(client, text="Первая", segment="active_7d"))
    assert len({a["op_id"], b["op_id"], c["op_id"]}) == 3
    assert len(_pending(pool)) == 3


def test_every_immediate_broadcast_door_adopts_the_bus_answer():
    """Каждое место, что заводит строку и ставит run_broadcast немедленно,
    снимает свою строку, если шина отдала чужую операцию."""
    import inspect

    from services import broadcaster, mini_app_broadcast, mini_app_widgets

    for src in (inspect.getsource(broadcaster.mass_broadcast_with_scheduling),
                inspect.getsource(broadcaster.resend_undelivered),
                inspect.getsource(broadcaster.ab_test_broadcast),
                inspect.getsource(mini_app_broadcast.setup_routes),
                inspect.getsource(mini_app_widgets.setup_routes)):
        assert "adopt_submitted_broadcast(" in src
