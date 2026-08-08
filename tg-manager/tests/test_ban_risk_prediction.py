"""Behavioral 6B — регрессия: предсказание риска бана подключено и осмысленно.

predict_ban_risk был корректной, но инертной функцией (0 call sites). Подключён
в account_monitor как раннее поведенческое предупреждение. Плюс исправлена
слепота fail_rate: считал провалы как result=='error', а op_worker (главный
писатель) пишет 'failed'.
"""
from __future__ import annotations

import inspect

import pytest


class _BanPool:
    """Pool, различающий запросы predict_ban_risk по тексту."""

    def __init__(self, recent, flood_cnt=0, acc=None):
        self._recent = recent
        self._flood = flood_cnt
        self._acc = acc

    async def fetch(self, q, *a):
        if "error_msg ILIKE" in q or "COUNT(*)" in q:
            return [{"cnt": self._flood}]
        return self._recent

    async def fetchrow(self, q, *a):
        return self._acc


@pytest.mark.asyncio
async def test_failed_results_counted_in_fail_rate():
    from services import behavioral_engine as be

    # 4 из 10 — 'failed' (словарь op_worker). Раньше (только 'error') fail_rate=0.
    recent = [{"result": "failed"}] * 4 + [{"result": "success"}] * 6
    pool = _BanPool(recent, flood_cnt=0, acc={"acc_status": "active", "trust_score": 0.8})
    res = await be.predict_ban_risk(pool, 1)
    assert any("failure rate" in r.lower() for r in res["reasons"]), res
    assert res["risk_score"] >= 30


@pytest.mark.asyncio
async def test_critical_on_flood_plus_spamblock():
    from services import behavioral_engine as be

    recent = [{"result": "success"}] * 5
    # 2 флуда (+40) + spamblock (+40) → ≥70 → critical
    pool = _BanPool(recent, flood_cnt=2, acc={"acc_status": "spamblock", "trust_score": 0.8})
    res = await be.predict_ban_risk(pool, 1)
    assert res["risk_level"] == "critical"
    assert res["risk_score"] >= 70


@pytest.mark.asyncio
async def test_no_activity_is_low_risk():
    from services import behavioral_engine as be

    res = await be.predict_ban_risk(_BanPool([]), 1)
    assert res["risk_level"] == "low"
    assert res["risk_score"] == 0


def test_ban_risk_wired_into_account_monitor():
    from services import account_monitor as am

    run_src = inspect.getsource(am.run)
    assert "_check_ban_risk" in run_src, "проверка риска бана не подключена в цикл"
    check_src = inspect.getsource(am._check_ban_risk)
    assert "predict_ban_risk" in check_src
    assert "critical" in check_src  # уведомляем только на критический уровень
