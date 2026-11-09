"""Одна метка здоровья аккаунта: Работает / Пауза / Нужно действие / Потерян."""
from __future__ import annotations

import os

from services import account_verdict as av

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(state, **kw):
    base = {"state": state, "reason": "причина", "ready_in_sec": None, "hint": None,
            "rehab_phase": None}
    base.update(kw)
    return base


def _row(status="active", active=True):
    return {"acc_status": status, "is_active": active}


def test_ready_is_working():
    v = av.verdict_for(_row(), _p("ready", ready_in_sec=0))
    assert v["level"] == "ok" and v["label"] == "Работает"
    assert v["reason"] == "готов к действиям"


def test_ready_with_trust_hint_keeps_the_hint():
    v = av.verdict_for(_row(), _p("ready", hint="доверие ниже порога"))
    assert v["level"] == "ok" and v["reason"] == "доверие ниже порога"


def test_cooling_and_quarantine_are_pause_with_time():
    for st in ("cooling", "quarantine"):
        v = av.verdict_for(_row(), _p(st, ready_in_sec=600))
        assert v["level"] == "pause" and v["label"] == "Пауза" and v["ready_in_sec"] == 600


def test_blocked_needs_action():
    v = av.verdict_for(_row(), _p("blocked", reason="прокси не отвечает"))
    assert v["level"] == "action" and v["reason"] == "прокси не отвечает"


def test_lost_statuses_are_lost_even_without_pulse():
    for st in ("banned", "deactivated", "deleted"):
        v = av.verdict_for(_row(st, active=False), None)
        assert v["level"] == "lost" and v["label"] == "Потерян"


def test_recoverable_dead_needs_action():
    for st in ("frozen", "session_expired", "spamblock"):
        v = av.verdict_for(_row(st), _p("dead"))
        assert v["level"] == "action", st


def test_spamblock_under_self_rehab_is_pause():
    for phase in ("appeal", "warming", "recheck"):
        v = av.verdict_for(_row("spamblock"), _p("dead", rehab_phase=phase))
        assert v["level"] == "pause", phase
    stuck = av.verdict_for(_row("spamblock"), _p("dead", rehab_phase="stuck"))
    assert stuck["level"] == "action"


def test_disabled_by_owner_is_off():
    v = av.verdict_for(_row(active=False), None)
    assert v["level"] == "off"


def test_no_pulse_no_verdict_for_active():
    assert av.verdict_for(_row(), None) is None


def test_every_pulse_state_maps_to_a_verdict():
    from services import fleet_pulse
    for st in fleet_pulse.STATE_ORDER:
        assert av.verdict_for(_row(), _p(st)) is not None, st


def test_summarize_counts_levels():
    s = av.summarize([{"level": "ok"}, {"level": "ok"}, {"level": "lost"}, None])
    assert s["ok"] == 2 and s["lost"] == 1 and s["action"] == 0


def test_bot_pulse_uses_the_same_labels():
    from bot.handlers import fleet_pulse as screen
    text = screen._render([_p("cooling", name="acc", state_label="⏳ пауза",
                              ready_in="5 мин", acc_status="active")])
    assert "Пауза: <b>1</b>" in text and "⏳ Пауза <b>acc</b>" in text


def test_wired_into_accounts_list_and_screen():
    api = open(os.path.join(ROOT, "services", "mini_app_accounts.py"), encoding="utf-8").read()
    assert "_verdict.annotate(pool, uid, rows)" in api
    js = open(os.path.join(ROOT, "mini_app", "screens", "accounts_list.js"), encoding="utf-8").read()
    assert "a.verdict && ACC_VERDICT_STYLE[a.verdict.level]" in js
