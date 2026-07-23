"""Регресс: долгоживущие фоновые циклы не запускаются fire-and-forget без ссылки.

Event loop держит на задачу ТОЛЬКО слабую ссылку (asyncio docs). Длинный фоновый
цикл, запущенный несохранённым `create_task`/`get_event_loop().create_task`, может
быть собран GC до завершения → фича молча умирает. Такие задачи обязаны иметь
удержанную ссылку (локальную в невозвращающемся фрейме, модульную или через
task_registry). Здесь стережём конкретные известные точки старта фоновых циклов.
"""
from __future__ import annotations

import inspect

from services import scheduler, auto_responder


def test_scheduler_ab_sweep_not_unreferenced():
    src = inspect.getsource(scheduler.run)
    assert "get_event_loop().create_task(declare_ab_winners" not in src
    assert "await declare_ab_winners(pool)" in src


def test_auto_responder_inactivity_sweep_has_ref():
    src = inspect.getsource(auto_responder.run)
    assert "get_event_loop().create_task(run_inactivity_sweep" not in src, (
        "fire-and-forget без ссылки → GC-риск для фонового sweep"
    )
    # ссылка удержана на уровне модуля (или иным способом), а не потеряна
    assert "_inactivity_sweep_task = asyncio.create_task(run_inactivity_sweep" in src, (
        "sweep должен запускаться с удержанием ссылки"
    )
    assert "_inactivity_sweep_task" in inspect.getsource(auto_responder), (
        "ссылка на sweep должна существовать на уровне модуля"
    )
