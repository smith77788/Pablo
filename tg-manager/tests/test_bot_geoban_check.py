"""Проверка ботов на ГЕО-БАН: ограничен в одних странах, доступен в других.

Telegram отдаёт флаг restricted/restriction_reason ОТНОСИТЕЛЬНО страны
смотрящего аккаунта (его прокси). Поэтому гео-бан виден только пробой с
аккаунтов РАЗНЫХ стран. classify_geoban — чистая логика сведения проб по
странам в вердикт; сторожим её и сквозную проводку (op + роуты + UI).
"""
from __future__ import annotations

from services.entity_restriction_check import (
    classify_geoban, GEO_BANNED, GEO_GLOBAL, GEO_CLEAN, GEO_INCONCLUSIVE,
)
from tests.miniapp_source import api_function, js_function


def _blocked(geo):
    return {"geo": geo, "restricted": True, "resolved": True, "has_username": True}


def _clean(geo):
    return {"geo": geo, "restricted": False, "resolved": True, "has_username": True}


def _unreachable(geo):
    return {"geo": geo, "resolved": False, "has_username": True}


def _failed(geo):
    return {"geo": geo, "probe_error": "flood wait of 30"}


# ── classify_geoban ──────────────────────────────────────────────────────────

def test_geo_banned_when_blocked_in_some_and_clean_in_others():
    v = classify_geoban([_blocked("RU"), _clean("DE")])
    assert v["status"] == GEO_BANNED
    assert v["blocked_geos"] == ["RU"] and v["clean_geos"] == ["DE"]


def test_unreachable_counts_as_blocked_for_public_bot():
    v = classify_geoban([_unreachable("RU"), _clean("US")])
    assert v["status"] == GEO_BANNED
    assert "RU" in v["blocked_geos"]


def test_global_ban_when_blocked_everywhere():
    v = classify_geoban([_blocked("RU"), _blocked("DE")])
    assert v["status"] == GEO_GLOBAL
    assert v["clean_geos"] == []


def test_clean_when_clean_in_two_plus_geos():
    v = classify_geoban([_clean("RU"), _clean("DE")])
    assert v["status"] == GEO_CLEAN


def test_inconclusive_with_single_geo():
    # одна страна не отличает гео-бан от общего — ни блок, ни чистота не доказуемы
    assert classify_geoban([_blocked("RU")])["status"] == GEO_INCONCLUSIVE
    assert classify_geoban([_clean("RU")])["status"] == GEO_INCONCLUSIVE


def test_failed_probes_do_not_count_as_geos():
    # сбой нашей стороны не вердикт: две страны только со сбоем → неубедительно
    assert classify_geoban([_failed("RU"), _failed("DE")])["status"] == GEO_INCONCLUSIVE


def test_within_geo_blocked_wins_over_clean():
    # две пробы одной страны: если хоть одна заблокирована — страна заблокирована
    v = classify_geoban([_clean("RU"), _blocked("RU"), _clean("DE")])
    assert v["status"] == GEO_BANNED and v["blocked_geos"] == ["RU"]


# ── сквозная проводка ────────────────────────────────────────────────────────

def test_executor_registered():
    import services.op_worker as ow
    import inspect
    src = inspect.getsource(ow)
    assert '"check_bot_geoban": _exec_check_bot_geoban' in src
    assert "async def _exec_check_bot_geoban" in src


def test_op_registered_in_bus():
    from services.operation_bus import OP_REGISTRY
    assert "check_bot_geoban" in OP_REGISTRY


def test_routes_registered():
    launch = api_function("check_bot_geoban")
    assert 'submit(pool, uid, "check_bot_geoban"' in launch
    targets = api_function("check_bot_geoban_targets")
    assert "geo_country" in targets  # считает различимые страны


def test_ui_has_geoban_button_and_runner():
    src = js_function("runBotGeoban")
    assert "/api/miniapp/check_bot_geoban" in src
    render = js_function("_shGeoRender")
    # гео-бан виден только при ≥2 странах — UI честно про это предупреждает
    assert "geos < 2" in render


# ── пострановый отчёт ────────────────────────────────────────────────────────

def test_verdict_carries_per_geo_map_and_blocked_reason():
    blocked = {"geo": "RU", "restricted": True, "resolved": True, "has_username": True,
               "restriction_reasons": [{"platform": "all", "reason": "geo",
                                        "text": "Недоступен в вашей стране"}]}
    v = classify_geoban([blocked, _clean("DE"), _failed("US")])
    assert v["per_geo"] == {"DE": "clean", "RU": "blocked", "US": "failed"}
    # Причина блокировки — текстом Telegram, а не кодом.
    assert v["blocked_reasons"] == {"RU": "Недоступен в вашей стране"}


def test_report_puts_problems_first_and_marks_every_geo():
    from services.entity_restriction_check import build_geo_report
    clean = classify_geoban([_clean("RU"), _clean("DE")])
    banned = classify_geoban([_blocked("RU"), _clean("DE")])
    text = build_geo_report(
        [{"kind": "bot", "title": "Помощник", "username": "help_bot", "verdict": clean},
         {"kind": "channel", "title": "Новости", "username": "news", "verdict": banned}],
        ["DE", "RU"])
    lines = text.splitlines()
    # Заблокированный канал идёт раньше чистого бота, хотя в списке был вторым.
    ch = next(i for i, ln in enumerate(lines) if "@news" in ln)
    bt = next(i for i, ln in enumerate(lines) if "@help_bot" in ln)
    assert ch < bt
    assert "Канал «Новости» (@news): DE ✅ · RU ⛔️" in lines[ch]
    assert "Бот «Помощник» (@help_bot): DE ✅ · RU ✅" in lines[bt]


def test_report_empty_without_rows():
    from services.entity_restriction_check import build_geo_report
    assert build_geo_report([], ["RU", "DE"]) == ""


# ── исполнитель: каналы и чаты проверяются наравне с ботами ─────────────────

class _GeoPool:
    def __init__(self, channels, bots, accounts):
        self._channels, self._bots, self._accounts = channels, bots, accounts
        self.logged: list = []
        self.fetch_queries: list = []

    async def fetch(self, query, *a):
        q = query.lower()
        self.fetch_queries.append(q)
        if "managed_channels" in q:
            return self._channels
        if "managed_bots" in q:
            return self._bots
        if "tg_accounts" in q:
            return self._accounts
        return []

    async def execute(self, query, *a):
        if "insert into operation_log" in query.lower():
            self.logged.append({"target": a[2], "status": a[3], "message": a[4]})
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


class _GeoClient:
    def __init__(self, geo):
        self.geo = geo

    async def disconnect(self):
        return None


def _geo_acc(i, geo):
    return {"id": i, "session_str": f"sess-{geo}", "geo_country": geo, "proxy_url": None}


def _run_geo_exec(pool, blocked_in, params=None):
    """blocked_in: {username: {geo, ...}} — где ресурс ограничен."""
    import asyncio
    import contextlib
    from unittest.mock import AsyncMock, patch
    from services import op_worker

    calls: list = []

    async def _connect(session_str, **kw):
        return _GeoClient(session_str.split("-", 1)[1])

    async def _resolve(client, *, kind, entity_id=None, username=None, **kw):
        calls.append((kind, username, client.geo))
        blocked = client.geo in blocked_in.get(username, set())
        return {"signals": {"has_username": True, "resolved": True,
                            "restricted": blocked, "restriction_reasons": []},
                "meta": {}}

    with contextlib.ExitStack() as st:
        st.enter_context(patch.object(op_worker, "_claim_available_accounts",
                                      AsyncMock(side_effect=lambda op, accs, o: accs)))
        st.enter_context(patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)))
        st.enter_context(patch.object(op_worker, "cancellable_pause", AsyncMock()))
        st.enter_context(patch.object(op_worker._infra_mem, "quarantined_accounts",
                                      AsyncMock(return_value=set())))
        st.enter_context(patch("services.account_manager.connect_client",
                               AsyncMock(side_effect=_connect)))
        st.enter_context(patch("services.entity_restriction_check.resolve_flags",
                               AsyncMock(side_effect=_resolve)))
        rec = st.enter_context(patch("services.shadowban_monitor._record_event", AsyncMock()))
        res = asyncio.run(op_worker._exec_check_bot_geoban(pool, None, 7, 99, params or {}))
    return res, calls, rec


def test_executor_checks_public_channels_groups_and_bots_by_geo():
    pool = _GeoPool(
        channels=[{"channel_id": 101, "username": "news", "title": "Новости", "type": "channel"},
                  {"channel_id": 102, "username": "chat", "title": "Чат", "type": "supergroup"}],
        bots=[{"bot_id": 201, "username": "help_bot", "first_name": "Помощник"}],
        accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    res, calls, rec = _run_geo_exec(pool, {"news": {"RU"}})

    assert res["status"] == "done" and res["checked"] == 3
    # Каждый ресурс открыт из каждой страны, и каналы — как каналы, а не как боты.
    assert sorted(calls) == sorted([
        ("channel", "news", "RU"), ("channel", "news", "DE"),
        ("group", "chat", "RU"), ("group", "chat", "DE"),
        ("bot", "help_bot", "RU"), ("bot", "help_bot", "DE")])
    assert res["counts"] == {GEO_BANNED: 1, GEO_CLEAN: 2}
    news = next(r for r in res["report"] if r["username"] == "news")
    assert news["per_geo"] == {"DE": "clean", "RU": "blocked"}
    assert "Канал «Новости» (@news): DE ✅ · RU ⛔️" in res["summary"]
    # Находка канала уходит в пульс с типом канала и его id, а не как бот.
    rec.assert_awaited_once()
    args, kwargs = rec.await_args
    assert args[2] == "channel_geoban"
    assert args[4]["channel_id"] == 101 and kwargs.get("bot_id") is None


def test_executor_skips_private_channels_and_respects_bot_only_selection():
    pool = _GeoPool(
        channels=[{"channel_id": 101, "username": "news", "title": "Новости", "type": "channel"}],
        bots=[{"bot_id": 201, "username": "help_bot", "first_name": "Помощник"}],
        accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    res, calls, _ = _run_geo_exec(pool, {}, params={"bot_ids": [201]})
    # Выбрали только ботов — каналы даже не запрашиваются.
    assert not any("managed_channels" in q for q in pool.fetch_queries)
    assert {c[0] for c in calls} == {"bot"}
    # А запрос каналов (когда он идёт) берёт только публичные.
    pool2 = _GeoPool(channels=[], bots=[], accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    _run_geo_exec(pool2, {})
    ch_q = next(q for q in pool2.fetch_queries if "managed_channels" in q)
    assert "username is not null" in ch_q


def test_routes_count_public_channels_too():
    launch = api_function("check_bot_geoban")
    assert "channel_ids" in launch and "managed_channels" in launch
    targets = api_function("check_bot_geoban_targets")
    assert "managed_channels" in targets and '"channels"' in targets
