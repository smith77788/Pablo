"""Канарейка Strike: остановить аккаунтные волны, если первые аккаунты уже ловят
флуд/бан — гейт массовой операции (сберечь флот), не убивая безаккаунтные векторы.

Без фикса staggered_strike гнал все волны (50% флота в первой) независимо от того,
что Telegram отвергает жалобы и банит — сжигая весь флот. Тест на pure-вердикт +
интеграция: при канарейка-стопе волны 2/3, сетевые узлы и SpamBot НЕ запускаются,
а abuse-формы (безаккаунтный вектор) — запускаются.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import strike_engine as se


# ── Чистый вердикт канарейки ────────────────────────────────────────────────────
def test_verdict_empty_and_single_never_abort():
    assert se.canary_verdict([]) == (False, "")
    # один аккаунт — не абортим (слишком мало сигнала)
    assert se.canary_verdict([{"_peer_flood": True}])[0] is False


def test_verdict_any_success_never_aborts():
    res = [{"peer_reported": True}, {"_peer_flood": True}]
    assert se.canary_verdict(res)[0] is False
    # успех может быть и через joined / msgs_reported
    assert se.canary_verdict([{"joined": True}, {"error": "FLOOD_WAIT 500"}])[0] is False
    assert se.canary_verdict([{"msgs_reported": 3}, {"error": "banned"}])[0] is False


def test_verdict_all_flood_aborts():
    abort, reason = se.canary_verdict([{"_peer_flood": True}, {"error": "PEER_FLOOD"}])
    assert abort is True and "флуд" in reason
    abort2, _ = se.canary_verdict(
        [{"error": "FLOOD_WAIT 1000"}, {"error": "USER_DEACTIVATED"}, {"_peer_flood": True}])
    assert abort2 is True


def test_verdict_mixed_danger_does_not_abort():
    # 0 успехов, но не ВСЕ опасны (сеть/приватность) → не абортим (ложный стоп хуже)
    res = [{"_peer_flood": True}, {"error": "network timeout"}]
    assert se.canary_verdict(res)[0] is False
    res2 = [{"error": "privacy restricted"}, {"error": "chat not found"}]
    assert se.canary_verdict(res2)[0] is False


# ── Интеграция: канарейка реально останавливает аккаунтные волны ────────────────
def _accs(n):
    return [{"id": i, "session_str": "s" * 20, "trust_score": 1.0} for i in range(1, n + 1)]


def _plan(accs, mode="normal"):
    return se.StrikePlan(
        targets=["@victim"], accounts=list(accs), reason="spam", preset=None,
        label="t", intel={}, waves=se.plan_waves(accs, 3),
        started_at=time.time(), phase="recon", mode=mode, owner_id=0)


def _install_mocks(monkeypatch, per_account_result):
    calls = {"strike": 0, "network": 0, "spambot": 0, "abuse": 0, "verify": 0,
             "wave_nums": []}

    async def fake_strike(acc, peer, intel, reason, preset, texts, idx, wave, sem,
                          mode="normal", pool=None):
        calls["strike"] += 1
        calls["wave_nums"].append(wave)
        return dict(per_account_result)

    async def fake_network(accounts, intel, reason, preset):
        calls["network"] += 1
        return {"nodes_attacked": 0, "total_reports": 0}

    async def fake_spambot(acc, target):
        calls["spambot"] += 1
        return {"status": "sent", "bots": {}}

    async def fake_abuse(target, cat, title="", members=0):
        calls["abuse"] += 1
        return {"ok": True, "submitted": 1, "total": 1}

    async def fake_verify(acc, target, max_attempts=3, delay_range=(25, 50)):
        calls["verify"] += 1
        return None

    async def fake_claim(ids):
        return list(ids)

    monkeypatch.setattr(se, "_one_account_strike", fake_strike)
    monkeypatch.setattr(se, "strike_network_nodes_v2", fake_network)
    monkeypatch.setattr(se, "_escalate_to_spambot", fake_spambot)
    monkeypatch.setattr(se, "submit_abuse_form", fake_abuse)
    monkeypatch.setattr(se, "verify_target_takedown", fake_verify)
    monkeypatch.setattr(se.random, "uniform", lambda a, b: 0)
    import services.op_worker as opw
    monkeypatch.setattr(opw, "try_claim_accounts", fake_claim)
    return calls


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_canary_abort_stops_account_waves_keeps_abuse(monkeypatch):
    # все аккаунты канарейки ловят peer-flood → аккаунтные волны должны встать
    calls = _install_mocks(monkeypatch, {"peer_reported": False, "_peer_flood": True,
                                         "error": "PEER_FLOOD"})
    plan = _plan(_accs(6))          # waves=[3,1,2]; канарейка = вся волна 1 (3)
    results = _run(se.staggered_strike(plan))
    r = results[0]
    assert r.canary_aborted is True
    # ударил ТОЛЬКО канарейкой (3), волны 2/3 не пошли
    assert calls["strike"] == se._CANARY_SIZE, calls
    assert set(calls["wave_nums"]) == {0}
    # аккаунтные векторы (сеть, spambot) пропущены — флот сбережён
    assert calls["network"] == 0 and calls["spambot"] == 0
    # безаккаунтный вектор (abuse-форма) — отработал
    assert calls["abuse"] == 1
    assert r.peer_reported == 0


def test_no_abort_when_canary_succeeds_runs_all_waves(monkeypatch):
    # канарейка успешна → идут все волны + сетевой вектор
    calls = _install_mocks(monkeypatch, {"peer_reported": True})
    plan = _plan(_accs(6))          # waves=[3,1,2]
    results = _run(se.staggered_strike(plan))
    r = results[0]
    assert r.canary_aborted is False
    assert calls["strike"] == 6      # 3 канарейка + 1 волна2 + 2 волна3
    assert set(calls["wave_nums"]) == {0, 1, 2}
    assert calls["network"] == 1 and calls["spambot"] == 1


def test_summary_surfaces_canary_abort():
    r = se.StrikeResult(target="@x", canary_aborted=True,
                        canary_reason="канарейка: 3/3 аккаунтов словили флуд/бан")
    out = se.format_strike_summary([r])
    assert "канарейка остановила флот" in out and "флот сбережён" in out
