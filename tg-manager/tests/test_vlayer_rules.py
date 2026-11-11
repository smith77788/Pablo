"""Правила модели поведения: владелец решает, что значит действие человека.

До этой правки все правила слоя были зашиты в код: «спросил цену» вело в
«Интерес», «Готов купить» жил 24 часа, аудитория грелась от 20 человек — и у
дорогого товара, и у доставки еды одинаково. Здесь проверяется, что правила
владельца реально меняют поведение слоя (сигнал, распад, каскад, окно
«вот-вот остынут»), что мусор в правилах не ломает слой, и что «Купил» нельзя
назначить догадке.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from services import virtual_layer as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _rules(**kw):
    raw = {}
    for k, v in kw.items():
        raw[k] = v
    return V.normalize_rules(raw)


# ── normalize_rules ──────────────────────────────────────────────────────────

def test_defaults_match_the_built_in_rules():
    d = V.normalize_rules(None)
    for sig, target in V.SIGNAL_TARGET.items():
        assert d["signal_target"][sig] == target
    for sig in V.NEGATIVE_SIGNALS:
        assert d["signal_target"][sig] == V.LOST
    assert d["ttl_hours"] == {r: V._TTL_HOURS[r] for r in V.TTL_RUNGS}
    assert d["audience"] == {"hot_at": "ready", "min_count": 20, "min_share": 0.15}
    assert d["bot"] == {"hot_at": V.BOT_HOT_AT, "min_count": 5, "min_share": 0.1}


def test_garbage_falls_back_to_defaults():
    d = V.normalize_rules(None)
    r = V.normalize_rules({
        "signal_target": {"asked_price": "purchased", "replied": "мусор",
                          "unknown_signal": "ready"},
        "ttl_hours": {"ready": "abc", "curious": -5, "interested": 10 ** 9},
        "audience": {"hot_at": "lost", "min_count": "x", "min_share": float("nan")},
        "cooling_window_hours": None,
    })
    assert r["signal_target"]["asked_price"] == d["signal_target"]["asked_price"]
    assert r["signal_target"]["replied"] == d["signal_target"]["replied"]
    assert "unknown_signal" not in r["signal_target"]
    assert r["ttl_hours"]["ready"] == d["ttl_hours"]["ready"]
    assert r["ttl_hours"]["curious"] == V.TTL_MIN_H
    assert r["ttl_hours"]["interested"] == V.TTL_MAX_H
    assert r["audience"] == d["audience"]
    assert r["cooling_window_hours"] == d["cooling_window_hours"]
    assert V.normalize_rules("не словарь") == d


def test_paid_always_leads_to_purchased():
    r = V.normalize_rules({"signal_target": {"paid": "off"}})
    assert r["signal_target"]["paid"] == "purchased"
    assert "paid" not in V.RULE_SIGNALS
    # Даже если правила подсунуты в обход normalize_rules.
    hacked = {"signal_target": {"paid": "off"}, "ttl_hours": {}}
    ch = V.apply_signal({"value": "ready", "confidence": 0.6}, "paid",
                        now=NOW, rules=hacked)
    assert ch["value"] == "purchased"


def test_no_signal_can_be_mapped_to_purchased():
    assert "purchased" not in V.RULE_TARGETS


# ── правила реально меняют поведение ─────────────────────────────────────────

def test_remapped_signal_moves_to_owner_stage():
    r = _rules(signal_target={"asked_price": "ready"})
    ch = V.apply_signal(None, "asked_price", now=NOW, rules=r)
    assert ch["value"] == "ready"
    # Без правил — прежнее поведение.
    assert V.apply_signal(None, "asked_price", now=NOW)["value"] == "interested"


def test_off_signal_is_ignored():
    r = _rules(signal_target={"unsubscribed": "off", "opened": "off"})
    cur = {"value": "interested", "confidence": 0.6,
           "expires_at": NOW + timedelta(hours=10)}
    assert V.apply_signal(cur, "unsubscribed", now=NOW, rules=r) is None
    assert V.apply_signal(None, "opened", now=NOW, rules=r) is None
    # Остальные негативы работают как прежде.
    assert V.apply_signal(cur, "blocked", now=NOW, rules=r)["value"] == V.LOST


def test_positive_signal_can_be_made_negative():
    r = _rules(signal_target={"asked_question": V.LOST})
    ch = V.apply_signal({"value": "curious", "confidence": 0.5}, "asked_question",
                        now=NOW, rules=r)
    assert ch["value"] == V.LOST


def test_ttl_from_rules_sets_expiry_and_decay():
    r = _rules(ttl_hours={"ready": 2})
    ch = V.apply_signal(None, "asked_how_to_pay", now=NOW, rules=r)
    assert ch["expires_at"] == NOW + timedelta(hours=2)
    # Распад по правилам: через 3 часа «готов» уже съехал, по стандарту (24 ч) — нет.
    cur = {"value": "ready", "confidence": 0.8,
           "expires_at": NOW + timedelta(hours=2)}
    later = NOW + timedelta(hours=3)
    assert V.decay(cur, now=later, rules=r)["value"] == "qualified"
    cur_default = {"value": "ready", "confidence": 0.8,
                   "expires_at": NOW + timedelta(hours=24)}
    assert V.decay(cur_default, now=later) is None


def test_decay_chain_uses_owner_ttl_for_next_rungs():
    # «Квалифицирован» у владельца живёт 1 час: просрочка «готов» на 2 часа
    # обязана опустить на две ступени, а не на одну.
    r = _rules(ttl_hours={"qualified": 1})
    cur = {"value": "ready", "confidence": 0.8, "expires_at": NOW}
    out = V.decay(cur, now=NOW + timedelta(hours=2), rules=r)
    assert out["value"] == "interested"


def test_cascade_kwargs_follow_rules():
    r = _rules(audience={"hot_at": "qualified", "min_count": 3, "min_share": 0.5})
    kw = V.cascade_kwargs(r, "audience")
    assert kw == {"hot_at": "qualified", "min_count": 3, "min_share": 0.5}
    assert V.cascade_from_counts({"qualified": 3, "new": 2}, **kw) == "hot"
    assert V.cascade_from_counts({"qualified": 3, "new": 2},
                                 **V.cascade_kwargs(None, "audience")) is None


def test_rules_diff_names_changes_in_russian():
    assert V.rules_diff(None) == []
    diff = V.rules_diff({"signal_target": {"asked_price": "ready"},
                         "ttl_hours": {"ready": 48}})
    assert any("спросил цену" in x and "Готов купить" in x for x in diff)
    assert any("48 ч" in x for x in diff)


# ── ручная ступень ───────────────────────────────────────────────────────────

def test_manual_change_can_go_down_and_out_of_terminal():
    assert V.manual_change({"value": "ready"}, "curious", now=NOW)["value"] == "curious"
    assert V.manual_change({"value": V.LOST}, "interested", now=NOW)["value"] == "interested"
    assert V.manual_change({"value": "purchased"}, V.LOST, now=NOW)["value"] == V.LOST
    assert V.manual_change({"value": "ready"}, "ready", now=NOW) is None
    with pytest.raises(ValueError):
        V.manual_change(None, "hot", now=NOW)


def test_manual_change_expiry_follows_rules():
    r = _rules(ttl_hours={"ready": 5})
    ch = V.manual_change(None, "ready", now=NOW, rules=r)
    assert ch["expires_at"] == NOW + timedelta(hours=5)
    assert ch["reason"] == "manual"
    assert V.manual_change({"value": "ready"}, "new", now=NOW)["expires_at"] is None
    assert V.manual_change(None, V.LOST, now=NOW)["expires_at"] is None
    assert V.reason_label("manual") == "владелец перевёл вручную"


# ── аналитика ────────────────────────────────────────────────────────────────

def test_leaks_ordered_by_ladder_and_worst_marked():
    out = V.leaks_from_rows([
        {"from_value": "ready", "lost": 1, "cooled": 5},
        {"from_value": "curious", "lost": 2, "cooled": 0},
        {"from_value": "qualified", "lost": 0, "cooled": 0},
        {"from_value": "hot", "lost": 9, "cooled": 9},
    ])
    assert [x["value"] for x in out] == ["curious", "ready"]
    assert out[1]["worst"] and not out[0]["worst"]
    assert out[1]["total"] == 6


def test_overall_conversion():
    conv = V.conversion_from_maxranks([{"maxr": 1, "c": 6}, {"maxr": 5, "c": 2}])
    assert V.overall_conversion(conv) == 25
    assert V.overall_conversion(V.conversion_from_maxranks([])) is None


def test_period_only_allows_known_values():
    assert V._period("30") == 30
    assert V._period(5) is None
    assert V._period("abc") is None
    assert V._period(None) is None


# ── чтение правил из базы ────────────────────────────────────────────────────

class _Pool:
    def __init__(self, raw=None, boom=False):
        self.raw, self.boom = raw, boom

    async def fetchval(self, q, *a):
        if self.boom:
            raise RuntimeError("база лежит")
        assert "vlayer_rules" in q
        return self.raw


def test_get_rules_reads_jsonb_string_and_fails_open():
    raw = json.dumps({"ttl_hours": {"ready": 7}})
    r = asyncio.run(V.get_rules(_Pool(raw), 1))
    assert r["ttl_hours"]["ready"] == 7
    assert asyncio.run(V.get_rules(_Pool(boom=True), 1)) == V.normalize_rules(None)
    assert asyncio.run(V.get_rules(_Pool("{битый"), 1)) == V.normalize_rules(None)


# ── проводка ─────────────────────────────────────────────────────────────────

def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_routes_and_screen_wired():
    from tests.miniapp_source import api_routes
    routes = api_routes()
    for route in ("/api/miniapp/vlayer/rules", "/api/miniapp/vlayer/rules/reset",
                  "/api/miniapp/vlayer/entity/{entity_type}/{entity_id}/stage"):
        assert route in routes
    html = _read("mini_app", "index.html")
    for frag in ("async function openVLayerRules", "async function openVLayerAnalytics",
                 "async function vlSetStage", 'id="vlTabs"',
                 "/api/miniapp/vlayer/rules", "/stage'"):
        assert frag in html
    assert "vlayer_rules" in _read("schema_v320_vlayer_rules.sql")


def test_runner_cascade_reads_owner_rules():
    src = _read("services", "organism", "runner.py")
    assert 'cascade_kwargs(_rules, "audience")' in src
    assert '"bot"))' in src
    assert "min_count=20, min_share=0.15" not in src


def test_signal_and_decay_read_owner_rules():
    src = _read("services", "virtual_layer.py")
    sig = src[src.index("async def signal(pool"):src.index("_SIGNALS_WORTH_A_CONTACT")]
    assert "await get_rules(pool, owner_id)" in sig and "rules=rules" in sig
    batch = src[src.index("async def _decay_batch"):src.index("async def run_decay")]
    assert "get_rules(pool, oid)" in batch and "rules=rules" in batch
