"""Ecosystem Auto-Management (Tier-1 4B) — регрессия.

Прежний auto_add_channels писал в ecosystem_channels — таблицу, которую никто
кроме мёртвых auto_*-функций не читает (реальное членство — ecosystem_members,
источник каналов — managed_channels). Фича была фиктивной. Тесты фиксируют
канонический путь, owner-scoped opt-in тумблер и его чтение.
"""
from __future__ import annotations

import pytest

from tests.test_executors import FakePool


class _UpdatedPool(FakePool):
    """execute() рапортует об обновлённой строке (UPDATE 1)."""

    async def execute(self, q, *a):
        self.calls.append(("execute", q))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_auto_add_uses_canonical_tables():
    from services import ecosystem_brain as eb

    # два кандидата-канала из managed_channels
    pool = FakePool(fetch=[{"channel_id": 111}, {"channel_id": 222}])
    res = await eb.auto_add_channels(pool, owner_id=5, ecosystem_id=9)
    assert res == {"added": 2, "total_new": 2}
    # запрос кандидатов бьёт по каноничным таблицам, а не по мёртвой ecosystem_channels
    fetch_q = [q for kind, q in pool.calls if kind == "fetch"][0]
    assert "managed_channels" in fetch_q
    assert "ecosystem_members" in fetch_q
    assert "ecosystem_channels" not in fetch_q
    # членов добавляли через ecosystem_members (add_member)
    inserts = [q for kind, q in pool.calls if kind == "execute"]
    assert any("ecosystem_members" in q for q in inserts)


@pytest.mark.asyncio
async def test_set_auto_manage_owner_scoped_true():
    from services import ecosystem_brain as eb

    pool = _UpdatedPool()
    assert await eb.set_auto_manage(pool, ecosystem_id=9, owner_id=5, enabled=True) is True
    q = [q for kind, q in pool.calls if kind == "execute"][-1]
    assert "owner_id = $2" in q  # owner-scoped
    assert "auto_manage" in q


@pytest.mark.asyncio
async def test_set_auto_manage_not_found_returns_false():
    from services import ecosystem_brain as eb

    # FakePool.execute → "UPDATE 0" (чужая/несуществующая экосистема)
    pool = FakePool()
    assert await eb.set_auto_manage(pool, 9, 5, True) is False


@pytest.mark.asyncio
async def test_is_auto_manage_enabled_reads_flag():
    from services import ecosystem_brain as eb

    assert await eb.is_auto_manage_enabled(FakePool(fetchrow={"am": "true"}), 9, 5) is True
    assert await eb.is_auto_manage_enabled(FakePool(fetchrow={"am": ""}), 9, 5) is False
    assert await eb.is_auto_manage_enabled(FakePool(fetchrow=None), 9, 5) is False


def test_auto_management_loop_wired_in_main():
    """Цикл авто-управления должен быть реально запущен в main.py."""
    import pathlib

    main_src = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    src = main_src.read_text(encoding="utf-8")
    assert "ecosystem_brain.run_auto_management" in src, "цикл 4B не запущен в main.py"
