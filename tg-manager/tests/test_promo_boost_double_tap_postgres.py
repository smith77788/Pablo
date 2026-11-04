"""Двойной тап «Накрутить» не оплачивает накрутку в SMM-панели дважды.

Запуск накрутки — платный заказ в чужой SMM-панели на деньги владельца, и
отменить его нельзя. Ручка мини-аппа и кнопка бота шли к панели без всякой
проверки: заказ в статусе 'boosting' запускался снова, а два одновременных
тапа (или бот и мини-апп разом) создавали два платных заказа.

Теперь заказ занимается атомарно ДО запроса к панели (`promo_claim_boost`).
Отказ панели возвращает заказ в прежний статус; таймаут — нет (панель могла
заказ принять), но аренда истекает сама, и заказ не застревает навсегда.

Нужен живой Postgres: проверяется настоящая гонка двух запросов.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993611

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    await p.execute("DELETE FROM promo_orders WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM smm_panels WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_warehouse WHERE owner_id=$1", OWNER)


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


class _Panel:
    """Подставная SMM-панель: считает платные заказы и отвечает с задержкой."""

    def __init__(self, answer):
        self.calls = 0
        self.answer = answer

    def make_client(self, api_url, api_key_enc):
        panel = self

        class _C:
            async def add_order(self, service_id, link, quantity):
                panel.calls += 1
                await asyncio.sleep(0.2)     # запрос к панели идёт по сети
                return dict(panel.answer)

        return _C()


@pytest.fixture
def setup(pool, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_stars, smm_panel

    _run(_cleanup(pool))
    bot_id = _run(pool.fetchval(
        "INSERT INTO bot_warehouse(owner_id, bot_username, status) "
        "VALUES($1,'promo_dbl_bot','ready') RETURNING id", OWNER))
    panel_id = _run(pool.fetchval(
        "INSERT INTO smm_panels(owner_id, name, api_url, api_key_enc, service_id) "
        "VALUES($1,'p','https://panel.invalid/api','key','1') RETURNING id", OWNER))
    order_id = _run(pool.fetchval(
        "INSERT INTO promo_orders(owner_id, keyword, bot_id, smm_panel_id, target_subs) "
        "VALUES($1,'кофе',$2,$3,100) RETURNING id", OWNER, bot_id, panel_id))

    fake = _Panel({"order": 4242})
    monkeypatch.setattr(smm_panel, "make_client", fake.make_client)
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: OWNER)

    app = web.Application()
    mini_app_stars.setup_routes(app, pool)

    async def _mk():
        c = TestClient(TestServer(app))
        await c.start_server()
        return c

    client = _run(_mk())
    yield client, fake, order_id
    _run(client.close())


def _boost(client, order_id):
    async def _go():
        resp = await client.post(f"/api/miniapp/promo/order/{order_id}/boost")
        return resp.status

    return _go()


def _status(pool, order_id) -> str:
    return _run(pool.fetchval("SELECT status FROM promo_orders WHERE id=$1", order_id))


def test_two_simultaneous_taps_pay_the_panel_once(pool, setup):
    client, fake, order_id = setup

    async def _both():
        return await asyncio.gather(_boost(client, order_id), _boost(client, order_id))

    statuses = sorted(_run(_both()))
    assert fake.calls == 1, (
        f"платных заказов в SMM-панели: {fake.calls} — двойной тап оплатил "
        "накрутку дважды")
    assert statuses == [200, 409]
    assert _status(pool, order_id) == "boosting"


def test_a_boosting_order_is_not_launched_again(pool, setup):
    client, fake, order_id = setup
    assert _run(_boost(client, order_id)) == 200
    assert _run(_boost(client, order_id)) == 409
    assert fake.calls == 1, "уже накручиваемый заказ отправлен в панель повторно"


def test_panel_refusal_returns_the_order_and_allows_a_retry(pool, setup):
    client, fake, order_id = setup
    fake.answer = {"error": "Not enough funds"}
    assert _run(_boost(client, order_id)) == 400
    assert _status(pool, order_id) == "waiting", "отказ панели не вернул заказ"
    fake.answer = {"order": 7}
    assert _run(_boost(client, order_id)) == 200
    assert fake.calls == 2


def test_timeout_keeps_the_lease_and_the_lease_expires(pool, setup):
    from database import db

    client, fake, order_id = setup
    fake.answer = {"error": "timeout"}
    assert _run(_boost(client, order_id)) == 504
    # Панель могла заказ принять: сразу повторить нельзя.
    assert _status(pool, order_id) == "launching"
    assert _run(_boost(client, order_id)) == 409
    assert fake.calls == 1
    # Аренда истекла — заказ снова можно запустить, он не застрял навсегда.
    _run(pool.execute(
        "UPDATE promo_orders SET updated_at = NOW() - make_interval(secs => $2) "
        "WHERE id=$1", order_id, float(db.PROMO_BOOST_LEASE_SEC + 5)))
    fake.answer = {"order": 9}
    assert _run(_boost(client, order_id)) == 200
    assert _status(pool, order_id) == "boosting"


def test_bot_button_uses_the_same_claim():
    import inspect

    from bot.handlers import promo_platform

    src = inspect.getsource(promo_platform.cb_order_boost)
    assert src.index("promo_claim_boost(") < src.index("add_order("), (
        "кнопка бота идёт в панель без занятия заказа — двойной тап в боте "
        "оплатит накрутку дважды")
