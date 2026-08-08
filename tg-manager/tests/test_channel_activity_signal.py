"""Сигнал активности канала (last_post_at) + разблокировка auto_remove (4B).

Раньше сигнала активности канала не было нигде в схеме (tg_channels.last_post_at
не существовал), поэтому auto_remove_dead_channels был неисполним. Добавлен
managed_channels.last_post_at (self-heal + запись при реальной публикации),
auto_remove переведён на него + ecosystem_members, с консервативной семантикой
NULL = не трогаем.
"""
from __future__ import annotations

import pathlib

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_auto_remove_uses_activity_signal_and_canonical_tables():
    from services import ecosystem_brain as eb

    pool = FakePool(fetch=[{"channel_id": 111}])
    res = await eb.auto_remove_dead_channels(pool, ecosystem_id=9, inactive_days=30)
    assert res == {"removed": 1, "total_dead": 1}
    q = [q for kind, q in pool.calls if kind == "fetch"][0]
    assert "ecosystem_members" in q
    assert "managed_channels" in q
    assert "last_post_at" in q
    # NULL last_post_at (никогда не постил) — не считаем мёртвым
    assert "last_post_at IS NOT NULL" in q
    # мёртвая таблица не используется
    assert "ecosystem_channels" not in q
    # удаляем из канонической таблицы членства
    dels = [q for kind, q in pool.calls if kind == "execute"]
    assert any("DELETE FROM ecosystem_members" in q for q in dels)


def test_publish_sets_last_post_at():
    """Реальная публикация должна проставлять last_post_at (сигнал активности)."""
    op = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
    src = op.read_text(encoding="utf-8")
    assert "UPDATE managed_channels SET last_post_at=now()" in src


def test_column_self_healed():
    """Колонка last_post_at должна добавляться self-heal'ом при старте."""
    api = pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
    src = api.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS last_post_at" in src


def test_auto_remove_wired_into_loop():
    from services import ecosystem_brain as eb
    import inspect

    src = inspect.getsource(eb.run_auto_management)
    assert "auto_remove_dead_channels" in src, "auto_remove не подключён в цикл 4B"
