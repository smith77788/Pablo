"""Паритет: отложенная mass_publish в боте (scheduled_for).

Раньше запланировать публикацию в каналы можно было только из mini-app
(schedule_post). Бот-хендлер mass_publish ставил только немедленные операции.
Теперь на превью есть «🕐 Запланировать» → ввод даты → op со scheduled_for
(op_worker его уважает: claim только когда scheduled_for<=now()).
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_preview_has_schedule_button():
    h = _read("bot/handlers/mass_publish.py")
    assert 'MassPubCb(action="schedule_start")' in h


def test_schedule_handlers_and_state_wired():
    h = _read("bot/handlers/mass_publish.py")
    assert 'MassPubCb.filter(F.action == "schedule_start")' in h
    assert "MassPublishFSM2.waiting_schedule" in h
    assert "waiting_schedule" in _read("bot/states.py")


def test_enqueue_helper_threads_scheduled_for():
    h = _read("bot/handlers/mass_publish.py")
    # общий helper, немедленный и отложенный путь используют его же
    assert "async def _enqueue_mass_publish(" in h
    assert "scheduled_for=scheduled_for" in h
    # отложенный путь передаёт ISO дату
    assert "scheduled_for=execute_at.isoformat()" in h


def test_op_worker_respects_scheduled_for():
    ow = _read("services/op_worker.py")
    assert "oq.scheduled_for IS NULL OR oq.scheduled_for <= now()" in ow


@pytest.mark.asyncio
async def test_enqueue_passes_scheduled_for_to_bus(monkeypatch):
    """_enqueue_mass_publish реально прокидывает scheduled_for в operation_bus."""
    import bot.handlers.mass_publish as mp
    from services import operation_bus

    captured = {}

    async def _fake_submit(pool, uid, op_type, params, *, total_items=0,
                           scheduled_for=None, **kw):
        captured.update(op_type=op_type, scheduled_for=scheduled_for,
                        total_items=total_items, params=params)
        return 999

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)

    class _Pool:
        async def fetchrow(self, q, *a):
            return {"cnt": 7}

    data = {"target_acc_ids": [1, 2], "delay_s": 10, "post_text": "hello"}
    op_id, total, mtype, mfid = await mp._enqueue_mass_publish(
        _Pool(), 42, data, scheduled_for="2026-12-25T18:30:00"
    )
    assert op_id == 999
    assert total == 7
    assert captured["op_type"] == "mass_publish"
    assert captured["scheduled_for"] == "2026-12-25T18:30:00"
    assert captured["params"]["account_ids"] == [1, 2]
