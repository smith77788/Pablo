"""Behavioral 6B — predict_campaign_success подключён на старт операции.

Функция была корректной, но инертной. Теперь оценка «ожидаемый успех ~X%»
показывается в уведомлении о старте операции (только при достаточной истории).
Плюс last_post_at проставляется и из bulk_post_to_channel.
"""
from __future__ import annotations

import pathlib

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_success_rate_and_confidence():
    from services import behavioral_engine as be

    # 8 прогонов, 6 done → 75%, confidence medium (>=5)
    hist = [{"status": "done", "duration": 5}] * 6 + [{"status": "failed", "duration": 3}] * 2
    res = await be.predict_campaign_success(FakePool(fetch=hist), owner_id=1, op_type="mass_publish")
    assert res["success_rate"] == pytest.approx(75.0)
    assert res["confidence"] == "medium"


@pytest.mark.asyncio
async def test_low_confidence_on_sparse_history():
    from services import behavioral_engine as be

    res = await be.predict_campaign_success(FakePool(fetch=[{"status": "done", "duration": 5}]),
                                            owner_id=1, op_type="strike")
    assert res["confidence"] == "low"  # <5 прогонов


@pytest.mark.asyncio
async def test_no_history_neutral():
    from services import behavioral_engine as be

    res = await be.predict_campaign_success(FakePool(fetch=[]), owner_id=1, op_type="x")
    assert res == {"success_rate": 50, "avg_duration_s": 0, "confidence": "low"}


def test_prediction_wired_into_op_start():
    op = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
    src = op.read_text(encoding="utf-8")
    assert "predict_campaign_success" in src, "оценка успеха не подключена на старт"
    # показываем только при достаточной уверенности
    assert '("medium", "high")' in src or "('medium', 'high')" in src


def test_bulk_post_sets_last_post_at():
    op = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
    src = op.read_text(encoding="utf-8")
    # last_post_at проставляется минимум в двух путях публикации
    assert src.count("UPDATE managed_channels SET last_post_at=now()") >= 2
