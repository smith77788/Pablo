"""Двойной тап по «Передать подарки» не отправляет те же подарки дважды.

Передача подарка необратима и тратит звёзды. От двойного тапа операции
защищает дедуп шины: повторный сабмит с ТЕМИ ЖЕ params сливается с первым. Но
ручка мини-аппа на каждый запрос заводит НОВЫЙ план, и params операции
(`{"plan_id": …}`) у двух тапов разные. Дедуп их не узнавал: два тапа (или
повтор клиента после таймаута) ставили две операции на одни и те же подарки.

Теперь подарок, который уже едет получателю в живой операции или уже передан
после последнего скана, во второй план не берётся, а выбор и постановка идут
под замком владельца. Подарки, чья передача отменена, и подарки, вернувшиеся
по новому скану, передать снова можно — замок не вечный.

Нужен живой Postgres: проверяется реальная гонка двух запросов и реальные
строки очереди, а заглушка пула SQL не исполняет.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993517
PHONE = "+70000993517"

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM gift_transfer_plans WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM gift_inventory WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE phone=$1", PHONE)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


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
    _run(_cleanup(p))
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gift_dbl') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture
def client(pool, monkeypatch):
    """Настоящие ручки подарков на настоящей базе; тариф и Ban Weather открыты."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_stars, operation_bus

    async def _no_plan_gate(*a, **k):
        return False

    async def _no_immunity(*a, **k):
        return None

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: OWNER)
    monkeypatch.setattr(operation_bus, "_enforce_min_plan", _no_plan_gate)
    monkeypatch.setattr(operation_bus, "_enforce_immunity", _no_immunity)

    app = web.Application()
    mini_app_stars.setup_routes(app, pool)
    async def _mk_client():
        c = TestClient(TestServer(app))
        await c.start_server()
        return c

    c = _run(_mk_client())
    _run(_cleanup(pool))
    _run(pool.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gift_dbl') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield c
    _run(c.close())


def _seed_gifts(pool, n: int = 2) -> list[int]:
    async def _mk():
        acc_id = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1,$2,'session',TRUE) RETURNING id", OWNER, PHONE)
        ids = []
        for i in range(n):
            ids.append(int(await pool.fetchval(
                "INSERT INTO gift_inventory(owner_id, account_id, gift_id, gift_type, "
                "   stars_cost, is_transferable, last_seen_at) "
                "VALUES($1,$2,$3,'Basic Star',25,TRUE, now() - interval '1 hour') "
                "RETURNING id", OWNER, acc_id, f"dbl-{i}")))
        return ids

    return _run(_mk())


def _post(client, ids):
    async def _go():
        resp = await client.post("/api/miniapp/gifts/transfer", json={
            "recipient": "@gift_receiver", "inventory_ids": ids,
            "payment_source": "stars"})
        return resp.status, await resp.json()

    return _go()


def _ops(pool) -> list[dict]:
    return [dict(r) for r in _run(pool.fetch(
        "SELECT id, status, params->>'plan_id' AS plan_id FROM operation_queue "
        "WHERE owner_id=$1 AND op_type='gift_transfer' ORDER BY id", OWNER))]


def test_two_simultaneous_taps_queue_one_transfer(pool, client):
    ids = _seed_gifts(pool)

    async def _both():
        return await asyncio.gather(_post(client, ids), _post(client, ids))

    results = _run(_both())
    statuses = sorted(s for s, _ in results)
    ops = _ops(pool)
    assert len(ops) == 1, (
        f"на одни и те же подарки встало операций: {len(ops)} — двойной тап "
        "передал бы каждый подарок дважды (дедуп шины не видит разные plan_id)")
    assert statuses == [200, 400], results
    refused = [body for s, body in results if s == 400][0]
    assert "уже передаются" in str(refused)


def test_a_repeat_after_the_first_tap_is_refused_and_preview_says_so(pool, client):
    ids = _seed_gifts(pool)
    s1, _ = _run(_post(client, ids))
    s2, body2 = _run(_post(client, ids))
    assert (s1, s2) == (200, 400), body2
    assert len(_ops(pool)) == 1

    async def _preview():
        resp = await client.post("/api/miniapp/gifts/transfer/preview", json={
            "recipient": "@gift_receiver", "inventory_ids": ids})
        return resp.status, await resp.json()

    st, body = _run(_preview())
    assert st == 400 and "уже передаются" in str(body), (
        "предпросмотр обещает стоимость за подарки, которые уже в пути")


def test_cancelled_transfer_does_not_lock_the_gifts_forever(pool, client):
    ids = _seed_gifts(pool)
    s1, _ = _run(_post(client, ids))
    assert s1 == 200
    _run(pool.execute(
        "UPDATE operation_queue SET status='cancelled', finished_at=now() "
        "WHERE owner_id=$1", OWNER))
    s2, body2 = _run(_post(client, ids))
    assert s2 == 200, f"после отмены подарки передать нельзя: {body2}"


def test_transferred_gift_is_locked_until_the_next_scan(pool, client):
    ids = _seed_gifts(pool, n=1)
    s1, _ = _run(_post(client, ids))
    assert s1 == 200
    # Исполнитель передал подарок, операция закрыта.
    _run(pool.execute(
        "UPDATE gift_transfer_items SET status='transferred', transferred_at=now() "
        "WHERE inventory_id=$1", ids[0]))
    _run(pool.execute(
        "UPDATE operation_queue SET status='done', finished_at=now() "
        "WHERE owner_id=$1", OWNER))
    # Окно дедупа шины тут ни при чём: свежий план = свежие params.
    s2, body2 = _run(_post(client, ids))
    assert s2 == 400, f"уже переданный подарок ставится в передачу снова: {body2}"

    # Новый скан снова увидел подарок у аккаунта (вернули) — передавать можно.
    _run(pool.execute(
        "UPDATE gift_inventory SET last_seen_at=now() + interval '1 second' "
        "WHERE id=$1", ids[0]))
    s3, body3 = _run(_post(client, ids))
    assert s3 == 200, f"вернувшийся по скану подарок заблокирован: {body3}"
