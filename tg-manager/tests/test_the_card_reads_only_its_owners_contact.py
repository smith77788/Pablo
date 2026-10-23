"""Лента и граф карточки читали подтаблицы чужого контакта по одному uuid.

НАХОДКА (23.10.2026). У карточки-360 два читателя: `/uch/contacts/{id}/timeline`
(лента касаний) и `/uch/contacts/{id}/identity` (граф идентичности). В обоих
contact_id берётся прямо из адреса, и в обоих ЧАСТЬ запросов владельца
проверяла, а часть — нет:

  * timeline: contact_history и organism_events — с owner_id; а
    `contact_sources WHERE contact_id=$1` и `contact_crm WHERE contact_id=$1` —
    без него вообще. По чужому uuid отдавались аккаунты-источники и стадия CRM;
  * identity: build_identity_graph владельца проверяет и на чужом возвращает
    {}, но следом безусловно звался `get_last_active(pool, cid)` — тоже без
    owner_id, — и ответ по чужому контакту был {"last_active": "Был сегодня"}.

Поэтому ответ выглядел не отказом, а правдой: полупустая лента и «был сегодня»
читаются как «у контакта мало событий», а не как «это не ваш контакт».

У contact_sources своего owner_id нет (schema_v138), так что скоуп может
держать только сам контакт: отсюда одна проверка `_own_contact` ДО чтения, а не
owner_id, дописанный в каждый запрос.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать `_own_contact` из uch_timeline -> красный
     test_a_strangers_timeline_is_not_found;
  2. убрать `_own_contact` из uch_identity -> красный
     test_a_strangers_identity_is_not_found;
  3. вернуть `get_last_active` без owner_id в запросе -> красный
     test_last_active_is_scoped_in_the_query_itself.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 991701
STRANGER = 991702


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _setup(pool):
    await pool.execute(
        "DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])",
        [OWNER, STRANGER])
    mine = await pool.fetchval(
        "INSERT INTO unified_contacts(owner_id, first_name, last_active_at) "
        "VALUES($1,'Свой', NOW()) RETURNING id", OWNER)
    theirs = await pool.fetchval(
        "INSERT INTO unified_contacts(owner_id, first_name, last_active_at) "
        "VALUES($1,'Чужой', NOW()) RETURNING id", STRANGER)
    await pool.execute(
        "INSERT INTO contact_crm(owner_id, contact_id, stage) VALUES($1,$2,'won')",
        STRANGER, theirs)
    return str(mine), str(theirs)


def _client(pool, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_uch
    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda request: int(request.headers["X-Uid"])
                        if request.headers.get("X-Uid") else None)
    app = web.Application()
    mini_app_uch.setup_routes(app, pool)
    return TestClient(TestServer(app))


def _ask(monkeypatch, path_for):
    """Открыть приложение на живом пуле и спросить свой и чужой контакт."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            mine, theirs = await _setup(pool)
            client = _client(pool, monkeypatch)
            await client.start_server()
            try:
                hdr = {"X-Uid": str(OWNER)}
                a = await client.get(path_for(mine), headers=hdr)
                ta = await a.text()
                b = await client.get(path_for(theirs), headers=hdr)
                tb = await b.text()
                return (a.status, ta), (b.status, tb)
            finally:
                await client.close()
        finally:
            await pool.close()
    return _run(go())


def test_a_strangers_timeline_is_not_found(monkeypatch):
    mine, theirs = _ask(
        monkeypatch, lambda c: f"/api/miniapp/uch/contacts/{c}/timeline")
    assert mine[0] == 200, mine
    assert theirs[0] == 404, (
        f"лента чужого контакта ответила {theirs[0]}: {theirs[1][:200]}")
    assert "won" not in theirs[1], "стадия CRM чужого контакта утекла в ленту"


def test_a_strangers_identity_is_not_found(monkeypatch):
    mine, theirs = _ask(
        monkeypatch, lambda c: f"/api/miniapp/uch/contacts/{c}/identity")
    assert mine[0] == 200, mine
    assert theirs[0] == 404, (
        f"граф чужого контакта ответил {theirs[0]}: {theirs[1][:200]}")
    assert "Был" not in theirs[1], (
        "последняя активность чужого контакта утекла: " + theirs[1][:200])


def test_last_active_is_scoped_in_the_query_itself(monkeypatch):
    """Проверка в обработчике не защищает второй вызов из другого места."""
    import asyncpg

    from services.contacts_hub.identity_engine import get_last_active

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            mine, theirs = await _setup(pool)
            return (await get_last_active(pool, mine, OWNER),
                    await get_last_active(pool, theirs, OWNER))
        finally:
            await pool.close()
    own, stranger = _run(go())
    assert own is not None, "своя активность пропала"
    assert stranger is None, (
        f"функция отдала активность чужого контакта: {stranger}")


def test_a_malformed_id_is_not_a_server_error(monkeypatch):
    """contact_id — uuid: мусор в адресе должен быть 404, а не 500.

    asyncpg биндит uuid без каста, поэтому до проверки формы такой адрес падал
    в except и отвечал «Внутренняя ошибка» — то есть владелец видел поломку
    сервера там, где просто нет такого контакта.
    """
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _setup(pool)
            client = _client(pool, monkeypatch)
            await client.start_server()
            try:
                r = await client.get(
                    "/api/miniapp/uch/contacts/not-a-uuid/timeline",
                    headers={"X-Uid": str(OWNER)})
                return r.status, await r.text()
            finally:
                await client.close()
        finally:
            await pool.close()
    status, body = _run(go())
    assert status == 404, f"мусорный id ответил {status}: {body[:200]}"
