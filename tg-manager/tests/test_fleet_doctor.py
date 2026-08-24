"""Диагностика флота: воронка отбора + вердикт «почему не исполняется»."""
from __future__ import annotations

import os

from services.fleet_doctor import (compute_funnel, verdict, classify_errors,
                                   _transport_verdict)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _acc(is_active=True, has_session=True, acc_status="active", cd_active=False,
         trust_score=1.0, in_operation=False):
    return {"is_active": is_active, "has_session": has_session, "acc_status": acc_status,
            "cd_active": cd_active, "trust_score": trust_score, "in_operation": in_operation}


def test_all_healthy_are_operable():
    f = compute_funnel([_acc(), _acc(), _acc()])
    assert f["total"] == 3 and f["operable_join"] == 3 and f["operable_invite"] == 3
    assert verdict(f)["ok"] is True


def test_drop_at_no_session():
    f = compute_funnel([_acc(has_session=False), _acc(has_session=False)])
    assert f["active"] == 2 and f["with_session"] == 0
    assert verdict(f)["key"] == "no_session"


def test_drop_at_dead_status():
    f = compute_funnel([_acc(acc_status="session_expired"), _acc(acc_status="banned")])
    assert f["with_session"] == 2 and f["alive"] == 0
    assert verdict(f)["key"] == "all_dead"


def test_drop_at_cooldown():
    f = compute_funnel([_acc(cd_active=True), _acc(cd_active=True)])
    assert f["alive"] == 2 and f["not_cooldown"] == 0
    assert verdict(f)["key"] == "all_cooldown"


def test_drop_at_low_trust():
    f = compute_funnel([_acc(trust_score=0.1), _acc(trust_score=0.2)])
    assert f["not_cooldown"] == 2 and f["operable_join"] == 0
    assert verdict(f)["key"] == "low_trust"


def test_invite_needs_higher_trust_than_join():
    # trust 0.4 годится для вступления (>=0.35), но не для инвайта (>=0.50)
    f = compute_funnel([_acc(trust_score=0.4)])
    assert f["operable_join"] == 1 and f["operable_invite"] == 0
    assert verdict(f)["ok"] is True   # join доступен → флот рабочий


def test_none_trust_defaults_to_full():
    f = compute_funnel([_acc(trust_score=None)])
    assert f["operable_invite"] == 1   # NULL trust трактуем как 1.0 (свежий аккаунт)


def test_verdicts_for_empty_and_inactive():
    assert verdict(compute_funnel([]))["key"] == "no_accounts"
    assert verdict(compute_funnel([_acc(is_active=False)]))["key"] == "all_inactive"


def test_classify_errors_detects_transport_and_authdup():
    # реальные тексты из диагностики пользователя
    assert classify_errors([{"error": "системный сбой ПОДКЛЮЧЕНИЯ ... транспорт (сеть/прокси/CF-релей/IPv6)"}])["class"] == "transport"
    assert classify_errors([{"error": "The authorization key was used under two different IP addresses"}])["class"] == "auth_dup"
    assert classify_errors([])["class"] == "none"
    assert classify_errors([{"error": ""}])["class"] == "none"


def test_transport_verdict_overrides_green_funnel():
    # воронка зелёная, но операции падают на подключении → вердикт НЕ «готов»
    tv = _transport_verdict("transport", {"no_proxy": 8})
    assert tv and tv["ok"] is False and tv["key"] == "transport_fail"
    av = _transport_verdict("auth_dup", {"no_proxy": 8, "cf_relay_configured": False,
                                         "ipv6_configured": False})
    assert av and av["ok"] is False and "прокси" in av["text"]
    assert _transport_verdict("none", {}) is None


def test_diagnose_override_wired():
    src = open(os.path.join(ROOT, "services", "fleet_doctor.py"), encoding="utf-8").read()
    assert "_transport_verdict(out[\"error_class\"][\"class\"]" in src
    assert "async def _transport_summary" in src
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "Транспорт (выход в сеть)" in html


def test_endpoint_route_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def fleet_diagnose" in api
    assert '"/api/miniapp/fleet/diagnose"' in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-fleetdoctor"' in html
    assert "function openFleetDoctor" in html and "/api/miniapp/fleet/diagnose" in html
    assert 'onclick="openFleetDoctor()"' in html
