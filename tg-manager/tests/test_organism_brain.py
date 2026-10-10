"""Мозг организма: из мир-снимка — межмодульные цепочки действий (чистое ядро)."""
from __future__ import annotations

from services.organism.brain import build_suggestions, narrative


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green"},
        "ops": {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": None,
        "growth": {"channels": 0, "growth_ops_7d": 0},
        "bots": {"total": 0, "active": 0, "inactive": 0,
                 "community_nodes": 0, "community_empty": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def _ids(sugs):
    return [s["id"] for s in sugs]


def test_hot_leads_chain_to_segment():
    s = build_suggestions(_snap(graph={"contacts": 500, "hot_leads": 12, "intents_24h": 3}))
    hot = next(x for x in s if x["id"] == "hot_offer")
    assert hot["action"]["kind"] == "segment_hot"      # намерение → сегмент-оффер
    assert "12" in hot["title"]
    assert "intents" in _ids(s)


def test_vault_off_is_urgent_and_first():
    s = build_suggestions(_snap(vault={"health": "disabled"}))
    assert s[0]["id"] == "vault_off" and s[0]["severity"] == "urgent"


def test_governor_red_warns():
    s = build_suggestions(_snap(fleet={"governor_level": "red", "governor_mult": 2.5,
                                        "pressure": 70, "active": 18}))
    assert "gov_red" in _ids(s)


def test_dead_accounts_and_no_goal():
    s = build_suggestions(_snap(fleet={"dead": 4, "active": 18, "governor_level": "green"}))
    assert "dead" in _ids(s) and "set_goal" in _ids(s)


def test_goal_present_replaces_set_goal():
    s = build_suggestions(_snap(goal={"goal": 1000, "label": "+1000 за 5 дн."}))
    assert "goal" in _ids(s) and "set_goal" not in _ids(s)


def test_idle_nudge_when_calm_and_free():
    s = build_suggestions(_snap())
    assert "idle" in _ids(s)


def test_dismissed_filtered():
    s = build_suggestions(_snap(fleet={"dead": 4, "active": 18}), dismissed={"dead"})
    assert "dead" not in _ids(s)


def test_failed_op_surfaces_with_opid():
    s = build_suggestions(_snap(ops={"running": 0, "pending": 0, "failed_24h": 1,
                                     "last_failed": {"op_id": 77, "op_type": "mass_invite",
                                                     "reason": "нет аккаунтов"}}))
    of = next(x for x in s if x["id"] == "op_fail")
    assert of["action"]["op_id"] == 77 and of["severity"] == "warn"


def test_narrative_mentions_fleet_and_signals():
    n = narrative(_snap(graph={"contacts": 500, "hot_leads": 12, "intents_24h": 3}))
    assert "Флот" in n and "12 горячих" in n and "давление" in n


def test_severity_order():
    s = build_suggestions(_snap(vault={"health": "disabled"}, fleet={"dead": 2, "active": 18},
                                graph={"hot_leads": 5, "contacts": 10}))
    sevs = [x["severity"] for x in s]
    order = {"urgent": 0, "warn": 1, "opportunity": 2, "info": 3}
    assert sevs == sorted(sevs, key=lambda x: order[x])   # отсортировано по важности


# ── Virtual Layer → действие ────────────────────────────────────────────────

def test_hot_ready_contacts_prompt_to_close():
    s = build_suggestions(_snap(vlayer={"ready": 7, "hot": 10, "audience": None}))
    v = next(x for x in s if x["id"] == "vlayer_hot")
    assert "7" in v["title"] and v["action"]["kind"] == "vlayer"


def test_few_ready_do_not_nag():
    """Порог 5 — не дёргать по одному лиду."""
    assert "vlayer_hot" not in _ids(build_suggestions(
        _snap(vlayer={"ready": 2, "hot": 3, "audience": None})))


def test_hot_audience_prompts_broadcast_when_no_ready_bucket():
    s = build_suggestions(_snap(vlayer={"ready": 0, "hot": 0, "audience": "hot"}))
    assert "vlayer_audience" in _ids(s)


def test_ready_bucket_takes_precedence_over_audience():
    """Конкретные готовые важнее общей температуры — одна подсказка, не две."""
    ids = _ids(build_suggestions(_snap(vlayer={"ready": 8, "hot": 8, "audience": "hot"})))
    assert "vlayer_hot" in ids and "vlayer_audience" not in ids


def test_quiet_layer_says_nothing():
    assert "vlayer_hot" not in _ids(build_suggestions(_snap()))
