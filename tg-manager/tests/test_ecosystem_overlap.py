"""Ecosystem 4A — регрессия: анализ пересечения аудитории на реальных данных.

analyze_audience_overlap корректна (читает channel_members), но эндпоинт
ecosystem_overlaps кормил её ch_ids из ecosystem_channels — мёртвой таблицы,
куда никто не пишет → ch_ids всегда пуст → overlap всегда {}. Фича была мертва
на стороне чтения. Исправлено на ecosystem_members (object_type='channel').
"""
from __future__ import annotations

import pathlib

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_overlap_computes_shared_audience():
    from services import ecosystem_brain as eb

    # ch1: {10,20}; ch2: {20,30} → пересечение {20}=1, объединение=3 → 33.3%
    pool = FakePool(fetch=[
        {"user_id": 10, "channel_id": 1},
        {"user_id": 20, "channel_id": 1},
        {"user_id": 20, "channel_id": 2},
        {"user_id": 30, "channel_id": 2},
    ])
    res = await eb.analyze_audience_overlap(pool, [1, 2])
    pair = res["pairs"][0]
    assert pair["shared"] == 1
    assert pair["overlap_pct"] == pytest.approx(33.3, abs=0.1)


@pytest.mark.asyncio
async def test_overlap_needs_two_channels():
    from services import ecosystem_brain as eb

    res = await eb.analyze_audience_overlap(FakePool(), [5])
    assert res["pairs"] == []


def test_endpoint_reads_ecosystem_members_not_dead_table():
    """ecosystem_overlaps должен читать членов из ecosystem_members, а не из
    мёртвой ecosystem_channels — иначе overlap всегда пустой."""
    api = pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
    src = api.read_text(encoding="utf-8")
    # локализуем регион эндпоинта overlaps
    idx = src.find("async def ecosystem_overlaps")
    assert idx != -1
    region = src[idx: idx + 1500]
    assert "ecosystem_members" in region
    assert "object_type='channel'" in region
    assert "FROM ecosystem_channels" not in region
