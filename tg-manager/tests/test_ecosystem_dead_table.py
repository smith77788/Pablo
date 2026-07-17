"""Ecosystem — регрессия: ретайр мёртвой таблицы ecosystem_channels.

ecosystem_channels — мёртвая параллель к реальной ecosystem_members (никто не
пишет). Читатели молча не работали. Здесь фиксируем, что подключённые пути
(channels-list) и auto_post_scheduling читают ecosystem_members по channel_id.
"""
from __future__ import annotations

import pathlib

import pytest

from tests.test_executors import FakePool


def _mini_app_src() -> str:
    p = pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
    return p.read_text(encoding="utf-8")


def test_channels_list_uses_ecosystem_members_by_channel_id():
    """Ветка «каналы из экосистем» списка каналов должна читать ecosystem_members
    и сравнивать по channel_id (Telegram id), а не по managed_channels.id (PK)."""
    src = _mini_app_src()
    # обе копии (SELECT и COUNT) переведены — мёртвой таблицы в них нет
    assert "FROM ecosystem_channels" not in src
    # членство берётся из ecosystem_members по object_id, сопоставляется channel_id
    assert "OR channel_id IN (" in src
    assert "em2.object_id FROM ecosystem_members em2" in src
    assert "em2.object_type='channel'" in src


@pytest.mark.asyncio
async def test_auto_post_scheduling_uses_canonical_tables():
    from services import ecosystem_brain as eb

    # 2 канала-члена; ни у одного нет активного mass_publish → оба «готовы»
    pool = FakePool(fetch=[{"id": 111}, {"id": 222}], fetchval=0)
    res = await eb.auto_post_scheduling(pool, ecosystem_id=9)
    assert res["channels_ready"] == 2
    assert res["scheduled"] == 2
    fetch_q = [q for kind, q in pool.calls if kind == "fetch"][0]
    assert "managed_channels" in fetch_q
    assert "ecosystem_members" in fetch_q
    assert "ecosystem_channels" not in fetch_q


@pytest.mark.asyncio
async def test_auto_post_skips_channels_with_pending_publish():
    from services import ecosystem_brain as eb

    # fetchval != 0 → у канала уже есть mass_publish в очереди → не считаем «готовым»
    pool = FakePool(fetch=[{"id": 111}], fetchval=1)
    res = await eb.auto_post_scheduling(pool, ecosystem_id=9)
    assert res["channels_ready"] == 1
    assert res["scheduled"] == 0
