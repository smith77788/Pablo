"""Счётчики «подписчики ботов владельца» после ускорения считают ТО ЖЕ, что раньше.

Зачем. Запросы вида
    FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
    WHERE mb.added_by=$1 AND bu.is_active=TRUE
Postgres исполнял полным проходом по bot_users ВСЕЙ платформы: на стенде с
1,5 млн подписчиков ~50 мс на каждый (под нагрузкой — 110 мс), а «Аудитория»
делала их четыре подряд. Теперь подписчики берутся по индексу через массив
ботов владельца (`bu.bot_id = ANY(ARRAY(SELECT bot_id FROM managed_bots ...))`):
«Аудитория» 349 → 14 мс, «Сводка в реальном времени» 124 → 16 мс,
«Что дальше» 153 → 48 мс, аналитический дашборд 53 → 14 мс.

JOIN и массив совпадают по смыслу, потому что bot_id в managed_bots
уникален, но переписанный запрос легко сделать быстрым и неверным: впустить
чужого бота, выключенного бота (аналитический дашборд считает только
включённые), неактивного подписчика или посчитать человека дважды. Эталон
здесь — ПРЕЖНИЕ запросы дословно, данные содержат каждую из этих ловушек, а
сравниваются ответы настоящих ручек и функций на живой базе.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990781
STRANGER = 990782
BOTS = (7990781001, 7990781002)
OFF_BOT = 7990781003          # бот владельца, выключенный
FOREIGN_BOT = 7990782001
TOKEN = "123456:owner-audience-test-token-abcdef"

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


_JOIN = "FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id WHERE mb.added_by=$1"
OLD = {
    "active": f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} AND bu.is_active=TRUE",
    "active_7d": (f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} AND bu.is_active=TRUE "
                  "AND bu.last_seen > NOW() - INTERVAL '7 days'"),
    "new_7d": (f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} "
               "AND bu.first_seen > NOW() - INTERVAL '7 days'"),
    "active_new_7d": (f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} AND bu.is_active=TRUE "
                      "AND bu.first_seen > NOW() - INTERVAL '7 days'"),
    "heatmap": (f"SELECT EXTRACT(HOUR FROM bu.last_seen)::int AS h, COUNT(*) AS c {_JOIN} "
                "AND bu.last_seen IS NOT NULL GROUP BY 1 ORDER BY 1"),
    "dash_total": (f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} AND mb.is_active = TRUE "
                   "AND bu.is_active = TRUE"),
    "dash_new_24h": (f"SELECT COUNT(DISTINCT bu.user_id) {_JOIN} AND mb.is_active = TRUE "
                     "AND bu.is_active = TRUE "
                     "AND bu.first_seen > NOW() - INTERVAL '24 hours'"),
}


async def _cleanup(p):
    ids = list(BOTS) + [OFF_BOT, FOREIGN_BOT]
    await p.execute("DELETE FROM bot_users WHERE bot_id = ANY($1::bigint[])", ids)
    await p.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", ids)


async def _seed(p):
    for bid, active, owner in ((BOTS[0], True, OWNER), (BOTS[1], True, OWNER),
                               (OFF_BOT, False, OWNER), (FOREIGN_BOT, True, STRANGER)):
        await p.execute(
            "INSERT INTO managed_bots (bot_id, token, username, added_by, is_active) "
            "VALUES ($1, $2, $3, $4, $5)", bid, f"aud-test-{bid}", f"aud_{bid}", owner, active)
    rows = []
    for u in range(1, 241):
        uid = 881000000 + u
        bots = [BOTS[u % 2]]
        if u % 3 == 0:
            bots.append(BOTS[(u + 1) % 2])      # тот же человек на двух ботах
        if u % 4 == 0:
            bots.append(OFF_BOT)                # и на выключенном боте
        if u % 5 == 0:
            bots.append(FOREIGN_BOT)            # и у чужого владельца
        for k, bid in enumerate(bots):
            rows.append((bid, uid,
                         (u * 13 + k * 7) % (60 * 24 * 20),        # first_seen, мин назад
                         (u * 29 + k) % (60 * 24 * 14),
                         u % 7 != 0 or k > 0))                     # is_active
    await p.executemany(
        "INSERT INTO bot_users (bot_id, user_id, first_seen, last_seen, is_active) "
        "VALUES ($1, $2, now() - make_interval(mins => $3), "
        "now() - make_interval(mins => $4), $5)",
        rows)


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
    _run(_seed(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


def _old(pool, key):
    if key == "heatmap":
        return [{"hour": int(r["h"]), "value": int(r["c"])}
                for r in _run(pool.fetch(OLD[key], OWNER))]
    return int(_run(pool.fetchval(OLD[key], OWNER)) or 0)


async def _get(pool, path):
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_auth, payment_webhook

    client = TestClient(TestServer(payment_webhook.make_app(pool, MagicMock())))
    await client.start_server()
    try:
        r = await client.get(path, headers={
            "Authorization": "Bearer " + mini_app_auth.make_token(OWNER, TOKEN)})
        assert r.status == 200, await r.text()
        return await r.json()
    finally:
        await client.close()


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MANAGER_BOT_TOKEN", TOKEN)


def test_the_traps_are_in_the_data(pool):
    """Контроль стенда: без расхождений между вариантами сравнение пустое."""
    assert _old(pool, "active") != _old(pool, "dash_total"), "нет выключенного бота"
    no_owner = int(_run(pool.fetchval(
        "SELECT COUNT(DISTINCT user_id) FROM bot_users WHERE bot_id = ANY($1::bigint[])",
        list(BOTS) + [OFF_BOT, FOREIGN_BOT])))
    assert no_owner > _old(pool, "active")
    assert _old(pool, "active") > 0 and _old(pool, "new_7d") > 0
    assert _old(pool, "active_7d") > 0 and _old(pool, "heatmap")


def test_audience_screen_matches_previous_queries(pool, token_env):
    d = _run(_get(pool, "/api/miniapp/audience_analytics"))
    assert d["total_users"] == _old(pool, "active")
    assert d["active_users"] == _old(pool, "active_7d")
    assert d["heatmap"] == _old(pool, "heatmap")


def test_realtime_dashboard_matches_previous_queries(pool, token_env):
    d = _run(_get(pool, "/api/miniapp/dashboard_realtime"))
    assert d["total_subscribers"] == _old(pool, "active")
    assert d["growth_7d"] == _old(pool, "active_new_7d")


def test_next_actions_state_matches_previous_query(pool):
    from services import next_actions

    state = _run(next_actions._gather_state(pool, OWNER))
    assert state["subscribers"] == _old(pool, "active")


def test_analytics_dashboard_skips_disabled_bots_as_before(pool):
    from services import analytics_dashboard

    d = _run(analytics_dashboard.get_dashboard_stats(pool, OWNER))
    assert d["audience"]["total_users"] == _old(pool, "dash_total")
    assert d["audience"]["new_24h"] == _old(pool, "dash_new_24h")
