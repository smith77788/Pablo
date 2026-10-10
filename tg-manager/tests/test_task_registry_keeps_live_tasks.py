"""Подметание реестра не отбирает у пользователя кнопку «стоп».

ЧТО БЫЛО. Периодическая уборка реестра активных задач удаляла запись, если
задача завершена ИЛИ работает дольше суток — вторая ветка была подписана как
«зависшая». Но удаление из реестра задачу НЕ останавливает: она продолжает
работать, а реестр — это ровно тот экран «Активные задачи», через который
пользователь её видит и жмёт «Отменить» (bot/handlers/active_tasks.py).

То есть уборка делала невидимой и неостанавливаемой именно ту задачу, которую
вероятнее всего надо остановить. Причём «дольше суток» само по себе не признак
беды: массовым операциям положено идти часами из-за пейсинга против банов, а
инвайт с продолжением на следующий день живёт больше суток штатно.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import task_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    task_registry._registry.clear()
    yield
    task_registry._registry.clear()


async def _forever():
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_long_running_task_stays_cancellable():
    task = asyncio.create_task(_forever())
    task_id = task_registry.register(7, "mass_invite", "Инвайт в @chat", task)
    # Состарим запись: задача идёт вторые сутки — штатно для инвайта с продолжением.
    task_registry._registry[7][task_id].started_at = time.time() - 30 * 3600

    sweep = asyncio.create_task(task_registry.run_cleanup_loop(interval=0.01))
    await asyncio.sleep(0.1)
    sweep.cancel()

    assert task_registry.list_tasks(7), (
        "долгая задача исчезла с экрана «Активные задачи» — пользователь больше "
        "не видит её и не может остановить, хотя она продолжает работать"
    )
    assert task_registry.cancel_task(7, task_id) is True, (
        "кнопка «Отменить» перестала работать именно для той задачи, ради "
        "которой она нужна"
    )
    task.cancel()


@pytest.mark.asyncio
async def test_finished_task_is_still_swept():
    """Обратная сторона: завершённые записи обязаны уходить, иначе реестр растёт."""
    async def _quick():
        return

    task = asyncio.create_task(_quick())
    task_id = task_registry.register(8, "warmup", "Разогрев", task)
    await task
    # Имитируем потерянный done-callback: запись осталась, хотя задача готова.
    task_registry._registry.setdefault(8, {})[task_id] = task_registry.TaskEntry(
        task_id=task_id, user_id=8, kind="warmup", label="Разогрев", task=task)

    sweep = asyncio.create_task(task_registry.run_cleanup_loop(interval=0.01))
    await asyncio.sleep(0.1)
    sweep.cancel()

    assert not task_registry._registry.get(8), "завершённая запись не убрана"


@pytest.mark.asyncio
async def test_cancel_all_reaches_a_long_running_task():
    task = asyncio.create_task(_forever())
    task_id = task_registry.register(9, "mass_invite", "Инвайт", task)
    task_registry._registry[9][task_id].started_at = time.time() - 48 * 3600

    assert task_registry.cancel_all(9) == 1
    task.cancel()
