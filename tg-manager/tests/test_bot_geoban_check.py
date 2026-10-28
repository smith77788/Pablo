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
