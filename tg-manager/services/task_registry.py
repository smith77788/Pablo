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
from typing import Optional


@dataclass
class TaskEntry:
    task_id: str
    user_id: int
    kind: str          # "strike", "mass_join", "mass_report", "warmup", etc.
    label: str         # human-readable description
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
        entry.task.cancel()
        return True
    return False


def cancel_all(user_id: int) -> int:
    """Cancel all active tasks for a user. Returns count cancelled."""
    count = 0
    for entry in list_tasks(user_id):
        entry.task.cancel()
        count += 1
    return count
