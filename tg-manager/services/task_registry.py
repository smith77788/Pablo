"""In-memory registry of running asyncio tasks per user.

Usage:
    from services.task_registry import register, cancel_task, list_tasks, cancel_all

    task = asyncio.create_task(my_coro())
    task_id = register(user_id, "strike", "Strike @target", task)
    ...
    cancel_task(user_id, task_id)
"""

from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TaskEntry:
    task_id: str
    user_id: int
    kind: str  # "strike", "mass_join", "mass_report", "warmup", etc.
    label: str  # human-readable description
    task: asyncio.Task
    started_at: float = field(default_factory=time.time)

    def is_done(self) -> bool:
        return self.task.done()

    def elapsed_str(self) -> str:
        secs = int(time.time() - self.started_at)
        if secs < 60:
            return f"{secs}с"
        return f"{secs // 60}м {secs % 60}с"


# Global registry: user_id → {task_id → TaskEntry}
_registry: dict[int, dict[str, TaskEntry]] = {}

# С какого возраста живая задача считается долгожителем и попадает в лог.
# Не порог удаления: удалять живую задачу нельзя (см. run_cleanup_loop).
_LONG_RUNNING_S = 24 * 3600


def register(user_id: int, kind: str, label: str, task: asyncio.Task) -> str:
    """Register a task and return its task_id."""
    task_id = uuid.uuid4().hex[:8]
    _registry.setdefault(user_id, {})[task_id] = TaskEntry(
        task_id=task_id,
        user_id=user_id,
        kind=kind,
        label=label,
        task=task,
    )
    task.add_done_callback(lambda _: _cleanup(user_id, task_id))
    return task_id


def _cleanup(user_id: int, task_id: str) -> None:
    bucket = _registry.get(user_id, {})
    bucket.pop(task_id, None)
    if not bucket:
        _registry.pop(user_id, None)


def list_tasks(user_id: int) -> list[TaskEntry]:
    """Return active (not done) tasks for a user."""
    bucket = _registry.get(user_id, {})
    return [e for e in bucket.values() if not e.is_done()]


def cancel_task(user_id: int, task_id: str) -> bool:
    """Cancel task by id. Returns True if found and cancelled."""
    entry = _registry.get(user_id, {}).get(task_id)
    if entry and not entry.is_done():
        entry.task.cancel(msg="user_requested")
        return True
    return False


def cancel_all(user_id: int) -> int:
    """Cancel all active tasks for a user. Returns count cancelled."""
    count = 0
    for entry in list_tasks(user_id):
        entry.task.cancel(msg="user_requested")
        count += 1
    return count


async def run_cleanup_loop(*, interval: int = 600) -> None:
    """Periodically clean up completed/dangling tasks from the registry.

    Runs every `interval` seconds (default: 10 min). Completed tasks are already
    cleaned by the done-callback, but tasks whose callback was lost (e.g. GC'd
    without firing) or that are done but not cleaned are swept here.
    Also removes entries for user buckets that have been empty for >1 hour
    (those should already be cleaned by _cleanup, but this is a safety net).

    ЧТО УБИРАЕТСЯ, А ЧТО НЕТ. Убирается только ЗАВЕРШЁННАЯ задача. Раньше сюда
    же попадала живая, работающая дольше суток — как «зависшая». Но удаление из
    реестра её не останавливает: задача продолжает работать, а реестр — это
    ровно тот экран «Активные задачи», через который пользователь её видит и
    жмёт «Отменить». То есть подметание отбирало кнопку «стоп» именно у той
    задачи, которую вероятнее всего надо остановить, и делало её невидимой.
    Долгая работа сама по себе не повод: массовым операциям положено идти
    часами (пейсинг против банов), и сутки для инвайта с продолжением — норма.
    Долгожителя теперь только логируем.
    """
    import logging

    log = logging.getLogger(__name__)
    last_empty_bucket_sweep: dict[int, float] = {}

    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            removed = 0

            # Sweep done tasks
            long_running = 0
            for user_id in list(_registry.keys()):
                bucket = _registry.get(user_id, {})
                for task_id in list(bucket.keys()):
                    entry = bucket[task_id]
                    if entry.is_done():
                        bucket.pop(task_id, None)
                        removed += 1
                    elif (now - entry.started_at) > _LONG_RUNNING_S:
                        # Живая задача-долгожитель. НЕ удаляем: удаление её не
                        # останавливает, а лишает пользователя кнопки «стоп»
                        # (см. докстринг). Сообщаем в лог и оставляем в реестре.
                        long_running += 1
                        log.warning(
                            "task_registry: задача %s (%s, %s) идёт %s — "
                            "оставлена в реестре, чтобы её можно было отменить",
                            task_id, entry.kind, entry.label[:60], entry.elapsed_str(),
                        )

                # Track empty buckets for delayed removal
                if not bucket:
                    if user_id not in last_empty_bucket_sweep:
                        last_empty_bucket_sweep[user_id] = now
                    elif now - last_empty_bucket_sweep[user_id] > 3600:
                        _registry.pop(user_id, None)
                        last_empty_bucket_sweep.pop(user_id, None)
                else:
                    last_empty_bucket_sweep.pop(user_id, None)

            # Отметки пустых корзин чистим по факту: корзину мог удалить
            # done-callback (_cleanup), и тогда её user_id сюда больше не
            # заглянет — словарь рос бы на каждого пользователя навсегда.
            for stale_uid in [u for u in last_empty_bucket_sweep if u not in _registry]:
                last_empty_bucket_sweep.pop(stale_uid, None)

            if removed or long_running:
                log.info(
                    "task_registry cleanup: убрано завершённых %d, живых долгожителей %d",
                    removed, long_running,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task_registry cleanup loop error: %s", e)
