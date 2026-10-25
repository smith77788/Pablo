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
import logging
from typing import Any, Coroutine

log = logging.getLogger(__name__)

# Strong-ссылки на активные фоновые задачи. done-callback снимает по завершении.
_bg_tasks: "set[asyncio.Task]" = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> "asyncio.Task | None":
    """Запустить корутину как фоновую задачу, удержав ссылку до завершения.

    Возвращает Task, либо None если нет запущенного event loop (sync-контекст) —
    вызывающий side-effect best-effort, отсутствие цикла не должно ронять поток.
    ``name`` — необязательная метка задачи (для отладки), как у create_task.
    """
    try:
        task = asyncio.create_task(coro, name=name)
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


async def drain(timeout: float = 5.0) -> dict:
    """Дать фоновым записям закончиться при остановке процесса. Не бросает.

    ЗАЧЕМ. Удержание ссылки спасает задачу от сборщика мусора, но не от конца
    процесса. Контейнер получает SIGTERM на КАЖДОМ деплое, и до сих пор
    остановка шла так: воркер сворачивался, пул закрывался, цикл закрывался —
    а запущенные через `spawn` записи всё ещё висели в нём. Их не ждал никто,
    поэтому они умирали на полпути: Python печатал «Task was destroyed but it
    is pending!», а запись не ложилась. Среди них не только телеметрия: там же
    запись в журнал соответствия, которая и завелась как аудит-след, терять
    который нельзя. Закрытый поверх живой задачи пул — отдельная беда: запрос
    падает на закрытом пуле и его ошибку никто не читает.

    Поэтому остановка ЖДЁТ эти задачи — коротко, перед закрытием пула. Ждать
    долго нельзя: платформа добьёт контейнер по своему таймауту, и тогда не
    успеет отработать вся остальная уборка. Что не уложилось — отменяем и
    говорим об этом громко: потерянная запись должна быть видна, а не тиха.

    Возвращает {"done": N, "cancelled": M} — для лога и тестов.
    """
    pending = [t for t in _bg_tasks if not t.done()]
    if not pending:
        return {"done": 0, "cancelled": 0}
    log.info("bg_tasks: жду %d фоновых записей до %.1fс", len(pending), timeout)
    try:
        await asyncio.wait(pending, timeout=max(0.0, float(timeout)))
    except Exception as e:  # защита уборки: ожидание не имеет права бросить
        log.warning("bg_tasks.drain: ожидание не удалось (%s)", str(e)[:120])
    left = [t for t in pending if not t.done()]
    for t in left:
        t.cancel()
    if left:
        log.error(
            "bg_tasks: %d фоновых записей НЕ успели за %.1fс и отменены — "
            "эти записи потеряны (%s)", len(left), timeout,
            ", ".join(sorted((t.get_name() or "без имени") for t in left))[:200],
        )
        try:
            from services import metrics as _m

            _m.inc("infragram_background_writes_dropped_total", {}, len(left))
        except Exception:
            pass
    return {"done": len(pending) - len(left), "cancelled": len(left)}
