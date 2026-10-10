"""Гео-свёртка флота (чистая) + гео-подсказка мозга организма."""
from __future__ import annotations

from services import geo_router
from services.organism.brain import build_suggestions


def test_summarize_empty():
    s = geo_router.summarize_distribution({})
    assert s["total"] == 0 and s["distinct"] == 0
    assert s["top"] is None and s["concentrated"] is False


def test_summarize_concentrated_flags_when_over_70pct():
    s = geo_router.summarize_distribution({"RU": 9, "DE": 1})
    assert s["top"] == "RU"
    assert s["top_share"] == 0.9
    assert s["concentrated"] is True
    assert s["distinct"] == 2


def test_summarize_not_concentrated_when_balanced():
    s = geo_router.summarize_distribution({"RU": 4, "DE": 4, "US": 4})
    assert s["concentrated"] is False
    assert s["top_share"] < 0.5


def test_summarize_small_fleet_never_concentrated():
    # <5 аккаунтов с гео — не считаем перекосом (мало данных).
    s = geo_router.summarize_distribution({"RU": 3})
    assert s["concentrated"] is False


def test_summarize_unknown_share():
    s = geo_router.summarize_distribution({"UNKNOWN": 6, "RU": 4})
    assert s["unknown"] == 6
    assert s["unknown_share"] == 0.6
    assert s["top"] == "RU"          # UNKNOWN не считается страной
    assert s["distinct"] == 1


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}},
        "ops": {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": None,
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_brain_suggests_diversify_on_concentration():
    geo = {"total": 10, "distinct": 2, "top": "RU", "top_share": 0.9,
           "unknown": 0, "unknown_share": 0.0, "concentrated": True}
    sugs = build_suggestions(_snap(fleet={"geo": geo}))
    ids = [s["id"] for s in sugs]
    assert "geo_concentrated" in ids
    geo_sug = next(s for s in sugs if s["id"] == "geo_concentrated")
    assert geo_sug["action"]["kind"] == "geo"


def test_brain_suggests_bind_proxies_when_unknown_heavy():
    geo = {"total": 10, "distinct": 1, "top": "RU", "top_share": 1.0,
           "unknown": 7, "unknown_share": 0.7, "concentrated": False}
    ids = [s["id"] for s in build_suggestions(_snap(fleet={"geo": geo}))]
    assert "geo_unknown" in ids


def test_brain_no_geo_suggestion_when_healthy():
    geo = {"total": 12, "distinct": 3, "top": "RU", "top_share": 0.4,
           "unknown": 1, "unknown_share": 0.08, "concentrated": False}
    ids = [s["id"] for s in build_suggestions(_snap(fleet={"geo": geo}))]
    assert "geo_concentrated" not in ids and "geo_unknown" not in ids
