"""Счётчик прогресса не должен «переполняться» при повторном прогоне (done>total).

Со скриншота: «Проверка 23 аккаунтов — 34/17» (done > total). Причина: _maybe_requeue
перезапускает операцию с ТЕМ ЖЕ op_id (status='pending', retry_count++), а done_items
не сбрасывался — при повторном прогоне per-item инкременты накапливались поверх
прошлого прогона. Фикс: _maybe_requeue сбрасывает done_items=0; исполнитель проверки
тоже обнуляет done_items при выставлении total_items (идемпотентный старт).
"""
from __future__ import annotations

import asyncio
import inspect
import re

from services import op_worker


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_maybe_requeue_resets_done_items():
    src = inspect.getsource(op_worker._maybe_requeue)
    # requeue-UPDATE обнуляет done_items вместе со сбросом в pending
    m = re.search(r"SET status='pending'.*?WHERE id=\$3", src, re.DOTALL)
    assert m, "requeue UPDATE не найден"
    assert "done_items=0" in m.group(0), "requeue должен сбрасывать done_items"


def test_check_executor_resets_done_on_start():
    src = inspect.getsource(op_worker._exec_check_accounts_health)
    assert "total_items=$1, done_items=0" in src, "исполнитель должен обнулять done_items при старте"


def test_stale_running_requeue_resets_done_items():
    """Пере-подхват зависшей 'running' op (старт воркера + watchdog) тоже обязан
    сбрасывать done_items — иначе счётчик копится поверх прошлого прогона."""
    for fn in (op_worker._reset_stale_running, op_worker._watchdog_stale):
        src = inspect.getsource(fn)
        assert "status = 'pending'" in src
        assert "done_items = 0" in src, f"{fn.__name__} должен сбрасывать done_items при requeue"


def test_circuit_release_resets_done_items():
    """Пере-подхват после открытия circuit breaker (_release_op_for_circuit) —
    тоже requeue в pending, обязан сбрасывать done_items (иначе done>total при
    повторном прогоне после cooldown). Раньше единственный requeue-путь без сброса."""
    src = inspect.getsource(op_worker._release_op_for_circuit)
    assert "status='pending'" in src
    assert "done_items=0" in src, "circuit-release requeue должен сбрасывать done_items"


def test_circuit_release_actually_issues_reset(monkeypatch):
    """Функционально: UPDATE при circuit-release реально содержит done_items=0."""
    captured = {}

    async def _fake_execute(pool, sql, *a, **k):
        if "status='pending'" in sql:
            captured["sql"] = sql
        return "UPDATE 1"

    monkeypatch.setattr(op_worker, "_safe_execute", _fake_execute)
    _run(op_worker._release_op_for_circuit(None, 42, 1800))
    assert "done_items=0" in captured.get("sql", ""), captured


def test_maybe_requeue_actually_issues_reset(monkeypatch):
    """Функционально: при ретраевой ошибке requeue-UPDATE реально содержит done_items=0."""
    captured = {}

    async def _fake_fetchrow(pool, sql, *a, **k):
        return {"retry_count": 0, "max_retries": 3}

    async def _fake_execute(pool, sql, *a, **k):
        if "status='pending'" in sql:
            captured["sql"] = sql
        return "UPDATE 1"

    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fake_fetchrow)
    monkeypatch.setattr(op_worker, "_safe_execute", _fake_execute)
    # ретраевая (не fatal/skip) ошибка
    ok = _run(op_worker._maybe_requeue(None, 7, RuntimeError("temporary blip"), {}, "check_accounts_health"))
    assert ok is True, "временная ошибка должна ставиться на повтор"
    assert "done_items=0" in captured.get("sql", ""), captured
