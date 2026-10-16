"""Честный per-account счётчик Strike: сколько аккаунтов приняли жалобу, сколько
упёрлись во флуд, сколько забанены, сколько прочий сбой.

Без него сводка показывала только суммарные жалобы — было не видно, что половина
флота словила флуд/бан (приукрашенная картина). Тест на классификатор исхода,
агрегацию и вывод в сводке.
"""
from __future__ import annotations

from services import strike_engine as se


# ── Классификатор исхода аккаунта ───────────────────────────────────────────────
def test_outcome_ok():
    assert se._acc_outcome({"peer_reported": True}) == "ok"
    assert se._acc_outcome({"joined": True}) == "ok"
    assert se._acc_outcome({"msgs_reported": 2}) == "ok"


def test_outcome_flood():
    assert se._acc_outcome({"_peer_flood": True}) == "flood"
    assert se._acc_outcome({"error": "FLOOD_WAIT 500"}) == "flood"
    assert se._acc_outcome({"error": "Too many requests"}) == "flood"


def test_outcome_banned():
    assert se._acc_outcome({"error": "USER_DEACTIVATED_BAN"}) == "banned"
    assert se._acc_outcome({"error": "auth_key duplicated"}) == "banned"
    assert se._acc_outcome({"error": "account is banned"}) == "banned"


def test_outcome_failed_and_empty():
    assert se._acc_outcome({"error": "channel not found"}) == "failed"
    assert se._acc_outcome({"error": "privacy restricted"}) == "failed"
    assert se._acc_outcome({}) == "failed"
    assert se._acc_outcome(None) == "failed"


# ── Агрегация ───────────────────────────────────────────────────────────────────
def test_aggregate_tallies_account_outcomes():
    results = [
        {"peer_reported": True},                 # ok
        {"peer_reported": True, "msg_reported": 3},  # ok
        {"_peer_flood": True},                    # flood
        {"error": "FLOOD_WAIT 60"},               # flood
        {"error": "USER_DEACTIVATED"},            # banned
        {"error": "network timeout"},             # failed
        None,                                     # failed
    ]
    agg = se.aggregate_results(results)
    assert agg["acc_ok"] == 2
    assert agg["acc_flood"] == 2
    assert agg["acc_banned"] == 1
    assert agg["acc_failed"] == 2
    # сумма категорий = числу аккаунтов (ничего не потеряно и не задвоено)
    total = agg["acc_ok"] + agg["acc_flood"] + agg["acc_banned"] + agg["acc_failed"]
    assert total == len(results)


# ── Вывод в сводке ──────────────────────────────────────────────────────────────
def test_summary_shows_honest_account_counter():
    r = se.StrikeResult(target="@x", unique_accounts=5, peer_reported=2,
                        accounts_ok=2, accounts_flood=2, accounts_banned=1,
                        accounts_failed=0)
    out = se.format_strike_summary([r])
    assert "Аккаунты:" in out
    assert "2</b> принято" in out and "2</b> флуд" in out and "1</b> бан" in out
