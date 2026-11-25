"""История оплат листается и называет итог — на живой базе.

РАЗРЫВ. Ручка отдавала `LIMIT 20` голым массивом, без общего числа и без
`offset`. Двадцать первая оплата для владельца не существовала, а именно её он
и ищет, когда сверяет списания с продлениями: «показано 20» и «всего 20» на
экране выглядят одинаково.

Живая база нужна, а не заглушка: у запроса появились `LIMIT $2 OFFSET $3` и
второй запрос-счётчик, а заглушка пула типы параметров не проверяет — ошибка
связывания была бы невидима.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995711
STRANGER = 995712


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    await pool.execute("DELETE FROM payments WHERE user_id = ANY($1::bigint[])",
                       [OWNER, STRANGER])


async def _seed(pool, n: int = 25):
    await _clean(pool)
    for i in range(n):
        await pool.execute(
            "INSERT INTO payments(user_id, plan, period_months, currency, "
            "wallet_address, reference, amount_crypto, amount_usd, status) "
            "VALUES($1,'pro',1,'TON','UQtest',$3,1.5,$2,'confirmed')",
            OWNER, 10 + i, f"pay-test-{OWNER}-{i}")
    # Чужая оплата: в счёт и в выдачу попасть не должна.
    await pool.execute(
        "INSERT INTO payments(user_id, plan, period_months, currency, "
        "wallet_address, reference, amount_crypto, amount_usd, status) "
        "VALUES($1,'pro',1,'TON','UQtest','pay-test-stranger',1.5,999,'confirmed')",
        STRANGER)


def _ask(monkeypatch, query: str, uid: int = OWNER) -> dict:
    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: uid)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool)
            app = web.Application()
            mini_app_api.setup_routes(app, pool)
            handler = next(
                r.handler for r in app.router.routes()
                if getattr(r.resource, "canonical", "") == "/api/miniapp/payments")
            resp = await handler(
                make_mocked_request("GET", "/api/miniapp/payments" + query))
            return json.loads(resp.body)
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


def test_first_page_says_how_much_history_there_is(monkeypatch):
    d = _ask(monkeypatch, "")
    assert len(d["items"]) == 20, d["page"]
    assert d["page"]["total"] == 25, "итог считается по всей истории владельца"
    assert d["page"]["has_more"] is True, "есть ещё пять оплат — экран обязан знать"
    assert d["page"]["offset"] == 0 and d["page"]["shown"] == 20


def test_the_next_page_continues_and_does_not_repeat(monkeypatch):
    first = [float(r["amount_usd"]) for r in _ask(monkeypatch, "?limit=20&offset=0")["items"]]
    second = _ask(monkeypatch, "?limit=20&offset=20")
    rest = [float(r["amount_usd"]) for r in second["items"]]
    assert len(rest) == 5
    assert not set(first) & set(rest), "страницы перекрылись — часть оплат видна дважды"
    assert second["page"]["has_more"] is False, "дальше ничего нет"


def test_the_order_is_stable_so_paging_does_not_skip_a_row(monkeypatch):
    """Сортировка по одной дате при равных значениях переставляет строки между
    запросами, и тогда догрузка теряет оплаты. Поэтому в ORDER BY есть id."""
    a = [float(r["amount_usd"]) for r in _ask(monkeypatch, "?limit=25&offset=0")["items"]]
    b = [float(r["amount_usd"]) for r in _ask(monkeypatch, "?limit=25&offset=0")["items"]]
    assert a == b and len(a) == 25


def test_another_owners_payment_is_neither_shown_nor_counted(monkeypatch):
    d = _ask(monkeypatch, "?limit=50")
    assert d["page"]["total"] == 25
    assert 999.0 not in [float(r["amount_usd"]) for r in d["items"]]


def test_a_stranger_sees_only_their_own(monkeypatch):
    d = _ask(monkeypatch, "?limit=50", uid=STRANGER)
    assert d["page"]["total"] == 1
    assert [float(r["amount_usd"]) for r in d["items"]] == [999.0]
