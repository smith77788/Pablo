"""Регресс: bg_tasks.spawn держит ссылку до завершения (класс 14) + разводка.

Проверяем поведенчески: задача, запущенная spawn(), НЕ теряется — она числится в
наборе, пока не завершится, и снимается по завершении. Плюс — что консеквентные
side-effect'ы (флаг in_operation в op_worker, запись конверсии воронки) идут через
spawn, а не через голый create_task без ссылки.
"""
from __future__ import annotations

import asyncio
import inspect

from services import bg_tasks


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_spawn_holds_ref_until_done():
    async def _scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def _work():
            started.set()
            await release.wait()

        task = bg_tasks.spawn(_work())
        await started.wait()
        # пока работает — ссылка удержана в наборе
        assert task in bg_tasks._bg_tasks
        release.set()
        await task
        # после завершения — снята done-callback'ом (нет утечки)
        assert task not in bg_tasks._bg_tasks

    _run(_scenario())


def test_spawn_no_running_loop_returns_none():
    # Вне event loop spawn не должен падать — best-effort side-effect.
    async def _noop():
        return None
    coro = _noop()
    assert bg_tasks.spawn(coro) is None  # корутина закрыта внутри, без RuntimeWarning


def test_op_worker_flag_uses_spawn():
    src = inspect.getsource(__import__("services.op_worker", fromlist=["_fire_db_flag"])._fire_db_flag)
    assert "spawn(" in src and "loop.create_task(_do_db_flag" not in src, (
        "флаг in_operation должен ставиться через spawn (удержание ссылки)"
    )


def test_funnel_conversion_uses_spawn():
    from services import funnel_runner
    src = inspect.getsource(funnel_runner)
    assert "spawn(" in src, "запись конверсии воронки должна идти через spawn"
    assert "asyncio.create_task(\n                        _record_funnel_conversion" not in src
