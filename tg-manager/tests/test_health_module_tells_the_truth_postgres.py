"""Модуль «Здоровье» не врёт о флоте — проверка на живой базе.

Что было на экране владельца 10.11.2026:

* вкладка «Инфраструктура» — три одинаковые карточки «Критично: Волна флудов
  48/50» восьмидневной давности. Детектор пишет запись эпизода каждые полчаса,
  а снимал старые записи ТОЛЬКО следующей записью того же типа: закончившаяся
  волна не снималась никогда, а каждая её получасовая запись была отдельной
  карточкой;
* вкладка «Аккаунты» в это же время — «✅ Норма»: статус смотрел только на бан
  и низкий траст;
* «На прогреве 49» при 34 аккаунтах: считались планы прогрева, переживающие
  свои аккаунты.

Нужен живой Postgres (`INFRAGRAM_TEST_DSN`): DISTINCT ON, коррелированный
NOT EXISTS и f-строки с общими условиями срезов на заглушке не проверить.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993871
OTHER = 993872

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    for o in (OWNER, OTHER):
        await p.execute("DELETE FROM anomaly_events WHERE owner_id=$1", o)
        await p.execute("DELETE FROM notification_dedup WHERE user_id=$1", o)
        await p.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", o)
        await p.execute(
            "DELETE FROM account_flood_log WHERE account_id IN "
            "(SELECT id FROM tg_accounts WHERE owner_id=$1)", o)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", o)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(_cleanup(pool))
    yield


async def _anomaly(p, owner, a_type, ago_min, severity="critical", title="Волна"):
    return await p.fetchval(
        "INSERT INTO anomaly_events(owner_id, anomaly_type, detector, severity, title, "
        "detected_at) VALUES($1,$2,'account',$3,$4, NOW() - make_interval(mins => $5)) "
        "RETURNING id", owner, a_type, severity, title, ago_min)


async def _account(p, owner, phone, **kw):
    cols = {"owner_id": owner, "phone": phone, "session_str": "s", "is_active": True}
    cols.update(kw)
    names = ", ".join(cols)
    ph = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    return await p.fetchval(
        f"INSERT INTO tg_accounts({names}) VALUES({ph}) RETURNING id", *cols.values())


# ── Оповещения ────────────────────────────────────────────────────────────────


def test_one_card_per_episode_not_one_per_half_hour(pool):
    from services import anomaly_detector as ad

    async def go():
        for m in (90, 60, 30, 5):
            await _anomaly(pool, OWNER, "flood_wave", m, title=f"Волна {m}")
        return await ad.get_active_anomalies(pool, OWNER)

    rows = _run(go())
    assert len(rows) == 1, f"эпизод разложен на {len(rows)} карточек"
    r = rows[0]
    assert r["occurrences"] == 4
    assert r["title"] == "Волна 5", "карточка обязана нести ТЕКУЩИЕ цифры эпизода"
    assert r["first_detected_at"] < r["last_detected_at"]


def test_ended_episode_is_not_shown_even_before_the_sweep(pool):
    """Восемь дней назад кончилась волна — её нет на экране."""
    from services import anomaly_detector as ad

    async def go():
        for m in (8 * 24 * 60 + 60, 8 * 24 * 60 + 30, 8 * 24 * 60):
            await _anomaly(pool, OWNER, "flood_wave", m)
        return await ad.get_active_anomalies(pool, OWNER)

    assert _run(go()) == []


def test_sweep_resolves_ended_episodes_and_keeps_live_ones(pool):
    from services import anomaly_detector as ad

    async def go():
        old = await _anomaly(pool, OWNER, "flood_wave", 8 * 24 * 60)
        live_old = await _anomaly(pool, OWNER, "error_spike", 90)
        live_new = await _anomaly(pool, OWNER, "error_spike", 10)
        other_live = await _anomaly(pool, OTHER, "flood_wave", 10)
        await ad.expire_ended_anomalies(pool)
        st = {r["id"]: r["is_active"] for r in await pool.fetch(
            "SELECT id, is_active FROM anomaly_events WHERE owner_id = ANY($1::bigint[])",
            [OWNER, OTHER])}
        return st, old, live_old, live_new, other_live

    st, old, live_old, live_new, other_live = _run(go())
    assert st[old] is False, "закончившийся эпизод остался активным навсегда"
    assert st[live_old] and st[live_new], "снят эпизод, который детектор ещё подтверждает"
    assert st[other_live], "живой эпизод другого владельца снят чужим"


def test_resolve_takes_the_whole_episode_and_only_own(pool):
    from services import anomaly_detector as ad

    async def go():
        ids = [await _anomaly(pool, OWNER, "flood_wave", m) for m in (60, 30, 5)]
        foreign = await _anomaly(pool, OTHER, "flood_wave", 5)
        assert not await ad.resolve_anomaly(pool, foreign, OWNER), "снял чужое"
        ok = await ad.resolve_anomaly(pool, ids[-1], OWNER)
        left = await ad.get_active_anomalies(pool, OWNER)
        foreign_active = await pool.fetchval(
            "SELECT is_active FROM anomaly_events WHERE id=$1", foreign)
        return ok, left, foreign_active

    ok, left, foreign_active = _run(go())
    assert ok
    assert left == [], "после «Снять» на место карточки встала предыдущая запись эпизода"
    assert foreign_active is True


def test_mute_is_visible_on_the_card(pool):
    from services import anomaly_detector as ad

    async def go():
        await ad.mute(pool, OWNER, "flood_wave", 24)
        return await ad.muted_until(pool, OWNER), await ad.muted_until(pool, OTHER)

    mine, other = _run(go())
    assert "flood_wave" in mine
    assert other == {}


# ── Итог флота и срезы ────────────────────────────────────────────────────────


def _health_counts(pool):
    """Тот же запрос, что у ручки /api/miniapp/health — через сам обработчик."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    import json

    from services import mini_app_api as api

    # Ручки берут _get_uid при регистрации — подменяем до неё.
    orig = api._get_uid
    api._get_uid = lambda _r: OWNER
    try:
        app = web.Application()
        api.setup_routes(app, pool)
        handler = next((r.handler for r in app.router.routes()
                        if r.method == "GET"
                        and getattr(r.resource, "canonical", "") == "/api/miniapp/health"), None)
        assert handler, "ручки /api/miniapp/health нет"
        resp = _run(handler(make_mocked_request("GET", "/api/miniapp/health", app=app)))
    finally:
        api._get_uid = orig
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


def test_status_is_not_norm_while_a_critical_alert_is_open(pool):
    _run(_account(pool, OWNER, "+79990000101"))
    _run(_anomaly(pool, OWNER, "flood_wave", 5))
    d = _health_counts(pool)
    v = d["verdict"]
    assert v["level"] == "crit", f"при открытой критичной тревоге статус: {v}"
    assert any(r["key"] == "alerts_crit" and r["open"] == "infra" for r in v["reasons"])


def test_cooldown_and_flood_make_the_status_say_so(pool):
    async def go():
        a = await _account(pool, OWNER, "+79990000102")
        await _account(pool, OWNER, "+79990000103")
        await pool.execute(
            "UPDATE tg_accounts SET cooldown_until = now() + interval '1 hour' WHERE id=$1", a)
        await pool.execute(
            "INSERT INTO account_flood_log(account_id, operation, flood_seconds) "
            "VALUES($1,'invite',300)", a)

    _run(go())
    d = _health_counts(pool)
    keys = {r["key"]: r for r in d["verdict"]["reasons"]}
    assert d["verdict"]["level"] == "warn"
    assert keys["cooldown"]["filter"] == "cooldown" and keys["cooldown"]["count"] == 1
    assert keys["flooded_24h"]["filter"] == "flooded"
    assert d["flooded_7d"] == 1 and d["active"] == 1


def test_warmup_counts_living_accounts_only(pool):
    async def go():
        a = await _account(pool, OWNER, "+79990000104")
        await pool.execute(
            "INSERT INTO account_warmup_plans(owner_id, account_id, status) VALUES($1,$2,'active')",
            OWNER, a)
        # План, переживший свой аккаунт: такого аккаунта в tg_accounts нет.
        await pool.execute(
            "INSERT INTO account_warmup_plans(owner_id, account_id, status) VALUES($1,$2,'active')",
            OWNER, 2_000_000_000 + OWNER)

    _run(go())
    assert _health_counts(pool)["warmup_active"] == 1


def test_every_health_slice_opens_exactly_what_it_counts(pool):
    """Плитка «Низкое доверие 1» открывает ровно одного, «Ловили флуд 1» — тоже."""
    from services.mini_app_api import _FLOODED_SQL, _LOW_TRUST_SQL

    async def go():
        low = await _account(pool, OWNER, "+79990000105", trust_score=0.2)
        await _account(pool, OWNER, "+79990000106", trust_score=0.9)
        fl = await _account(pool, OWNER, "+79990000107")
        await pool.execute(
            "INSERT INTO account_flood_log(account_id, operation, flood_seconds, created_at) "
            "VALUES($1,'invite',60, now() - interval '3 days')", fl)
        lows = [r["id"] for r in await pool.fetch(
            f"SELECT id FROM tg_accounts WHERE owner_id=$1 AND {_LOW_TRUST_SQL}", OWNER)]
        floods = [r["id"] for r in await pool.fetch(
            f"SELECT id FROM tg_accounts WHERE owner_id=$1 AND {_FLOODED_SQL}", OWNER)]
        return low, fl, lows, floods

    low, fl, lows, floods = _run(go())
    d = _health_counts(pool)
    assert lows == [low] and d["low_trust"] == 1
    assert floods == [fl] and d["flooded_7d"] == 1
