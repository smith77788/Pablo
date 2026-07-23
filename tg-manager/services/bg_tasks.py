"""Запуск фоновых задач с удержанием ссылки (класс 14: fire-and-forget → GC-риск).

Event loop держит на задачу, созданную `asyncio.create_task`, лишь СЛАБУЮ ссылку
(asyncio docs: «A task that isn't referenced elsewhere may get garbage collected at
any time, even before it's done»). Несохранённый create_task для side-effect (запись
телеметрии/конверсии, апдейт флага) может быть собран GC до завершения → эффект молча
теряется. `spawn()` держит strong-ссылку в модульном наборе до завершения задачи.

Использование:
    from services.bg_tasks import spawn
    spawn(record_something(pool, ...))   # огонь-и-забыл, но с удержанием ссылки
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

# Strong-ссылки на активные фоновые задачи. done-callback снимает по завершении.
_bg_tasks: "set[asyncio.Task]" = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> "asyncio.Task | None":
    """Запустить корутину как фоновую задачу, удержав ссылку до завершения.

    Возвращает Task, либо None если нет запущенного event loop (sync-контекст) —
    вызывающий side-effect best-effort, отсутствие цикла не должно ронять поток.
    """
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # Нет запущенного event loop — закрываем корутину, чтобы не течь.
        try:
            coro.close()
        except Exception:
            pass
        return None
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task
