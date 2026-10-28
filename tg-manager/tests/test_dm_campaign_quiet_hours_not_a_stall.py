"""Длинная DM-рассылка, попавшая в ночь аккаунтов, НЕ должна падать «Ошибкой».

Баг: кампания спит в своём цикле (тихие часы — до 10ч, флуд-паузы), эти паузы
не двигают done_items и не объявлялись сторожу застоя op_worker. Сторож (90 мин)
принимал ночное ожидание за зависание и ронял здоровую рассылку как
«Операция была прервана… перестала двигаться». Так «рассылки всегда падали».

Фикс: dm_engine._guarded_sleep объявляет паузу (op_worker.declare_op_pause), а
развилка потолка прогона в _run_with_stall_guard уважает объявленную паузу —
продолжает штатно, а не ошибкой.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import op_worker, dm_engine


# ── declare/clear API ────────────────────────────────────────────────────────

def test_declare_and_clear_op_pause():
    op_id = 424242
    op_worker.clear_op_pause(op_id)
    op_worker.declare_op_pause(op_id, 50)
    assert op_worker._declared_pause_until.get(op_id, 0.0) > time.monotonic()
    op_worker.clear_op_pause(op_id)
    assert op_id not in op_worker._declared_pause_until


def test_declare_op_pause_fail_open_on_bad_id():
    # None/мусорный op_id не должен ронять исполнителя
    op_worker.declare_op_pause(None, 10)
    op_worker.declare_op_pause("нечисло", 10)  # type: ignore[arg-type]


# ── _guarded_sleep объявляет паузу ───────────────────────────────────────────

def test_guarded_sleep_declares_pause():
    op_id = 525252
    op_worker.clear_op_pause(op_id)
    asyncio.run(dm_engine._guarded_sleep(op_id, 0.01))
    # после сна окно паузы ещё открыто (сон + буфер) — сторож её увидит
    assert op_worker._declared_pause_until.get(op_id, 0.0) > time.monotonic()
    op_worker.clear_op_pause(op_id)


def test_guarded_sleep_no_op_id_just_sleeps():
    # без op_id — просто сон, без объявления и без падения
    asyncio.run(dm_engine._guarded_sleep(None, 0.0))


# ── потолок прогона во время объявленной паузы = штатное продолжение ──────────

def _run_guard_until_ceiling(op_id: int, declared: bool, monkeypatch):
    async def _fake_fetchrow(*_a, **_k):
        return {"done_items": 0}
    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fake_fetchrow)

    async def _busy():
        await asyncio.sleep(5)  # дольше крошечного потолка

    async def _run():
        op_worker.clear_op_pause(op_id)
        if declared:
            op_worker.declare_op_pause(op_id, 100)  # активная осознанная пауза
        try:
            # timeout_s=0.1 → почти сразу упираемся в потолок прогона
            await op_worker._run_with_stall_guard(None, _busy(), op_id, "dm_campaign", 0.1)
        finally:
            op_worker.clear_op_pause(op_id)

    return _run


def test_ceiling_during_declared_pause_resumes_gracefully(monkeypatch):
    """В объявленной паузе (ночь/флуд) потолок = _OpOverCeiling (продолжение),
    даже если до паузы не ушло ни одной цели (done==0)."""
    with pytest.raises(op_worker._OpOverCeiling):
        asyncio.run(_run_guard_until_ceiling(636363, declared=True, monkeypatch=monkeypatch)())


def test_ceiling_without_progress_and_without_pause_is_hard_timeout(monkeypatch):
    """Без прогресса и без объявленной паузы потолок = зависание (TimeoutError)."""
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run_guard_until_ceiling(747474, declared=False, monkeypatch=monkeypatch)())


# ── цикл кампании использует _guarded_sleep, а не голый asyncio.sleep ─────────

def test_campaign_loop_uses_guarded_sleep():
    import inspect
    src = inspect.getsource(dm_engine.run_campaign)
    assert "_guarded_sleep(op_id" in src, "паузы цикла кампании должны объявляться сторожу"
    assert "await asyncio.sleep(" not in src, (
        "в run_campaign не должно остаться необъявленных asyncio.sleep — "
        "они выглядят застоем и роняют рассылку «Ошибкой»"
    )
