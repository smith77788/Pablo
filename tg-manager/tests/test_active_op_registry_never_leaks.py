"""Реестр активных операций освобождается всегда — иначе владелец встаёт намертво.

ЧТО БЫЛО. Поллер добавлял id операции в `_active_op_ids` и запускал
`_run_op_task` отдельной задачей. Снятие id стоит в `finally` внутри
`async with owner_sem:` — то есть накрывает не всю функцию. Пролог до семафора
(разбор params, чтение предохранителя, само ожидание семафора) остаётся
снаружи, и любое исключение там оставляло id в реестре НАВСЕГДА.

ПОЧЕМУ ЭТО ХУДШИЙ ВИД УТЕЧКИ. По членству в `_active_op_ids` операцию ЩАДЯТ
оба сторожа зависших (`_watchdog_stale`, `recovery_engine._queue_recovery`) и
алерт о застрявших: они специально не трогают то, что «сейчас исполняется».
Утёкшая операция поэтому вечно числится 'running', держит слот параллельности
владельца и при этом не видна как проблема ни одному механизму. Три таких
случая — и у владельца не стартует вообще ничего до перезапуска процесса.

Вдобавок исключение пролога исчезало бесследно: `create_task` без обработчика
прячет ошибку до сборки мусора, а в очереди операция оставалась 'running'.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


def _func(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"в op_worker.py нет функции {name}")


def test_poller_starts_operations_only_through_the_guard():
    """Поллер обязан звать обёртку: только она гарантирует снятие с реестра."""
    started = [
        n.func.id
        for n in ast.walk(_func("_process_pending"))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id.startswith("_run_op_task")
    ]
    assert started, "поллер вообще не запускает операций — проверь имя вызова"
    assert set(started) == {"_run_op_task_guarded"}, (
        "поллер запускает операцию в обход обёртки: исключение в прологе "
        f"оставит id в _active_op_ids навсегда (вызовы: {started})"
    )


def test_guard_releases_the_registry_in_a_top_level_finally():
    """Снятие с реестра должно стоять в finally самой обёртки, а не глубже."""
    fn = _func("_run_op_task_guarded")
    tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "в _run_op_task_guarded нет finally на верхнем уровне тела"
    finals = "\n".join(ast.dump(s) for t in tries for s in t.finalbody)
    assert "_active_op_ids" in finals and "discard" in finals, (
        "finally обёртки не снимает op_id с _active_op_ids — утечка сохранилась"
    )


class _Pool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []


class _Bot:
    pass


@pytest.fixture
def worker(monkeypatch):
    from services import op_worker

    op_worker._active_op_ids.clear()
    yield op_worker
    op_worker._active_op_ids.clear()


def _row(op_id: int = 4242) -> dict:
    return {
        "id": op_id,
        "owner_id": 555,
        "op_type": "bulk_join",
        "params": "{}",
    }


@pytest.mark.asyncio
async def test_prologue_crash_releases_the_registry(worker, monkeypatch):
    async def _boom(pool, bot, row):
        raise RuntimeError("предохранитель недоступен")

    async def _requeue(*a, **kw):
        return False

    monkeypatch.setattr(worker, "_run_op_task", _boom)
    monkeypatch.setattr(worker, "_maybe_requeue", _requeue)

    worker._active_op_ids.add(4242)
    await worker._run_op_task_guarded(_Pool(), _Bot(), _row())

    assert 4242 not in worker._active_op_ids, (
        "операция осталась в реестре активных: оба сторожа и алерт о застрявших "
        "будут её пропускать, слот владельца занят навсегда"
    )


@pytest.mark.asyncio
async def test_prologue_crash_is_not_swallowed_but_written_as_failure(worker, monkeypatch):
    pool = _Pool()

    async def _boom(pool, bot, row):
        raise RuntimeError("битый params")

    async def _requeue(*a, **kw):
        return False

    monkeypatch.setattr(worker, "_run_op_task", _boom)
    monkeypatch.setattr(worker, "_maybe_requeue", _requeue)

    worker._active_op_ids.add(4242)
    await worker._run_op_task_guarded(pool, _Bot(), _row())

    writes = [q for q, _ in pool.executed if "status='failed'" in q]
    assert writes, "операция навсегда осталась бы 'running' — терминальный статус не записан"
    assert "status NOT IN" in writes[0], (
        "запись статуса не защищена от гонки: она может затереть уже "
        "выставленный терминальный статус (например отмену владельцем)"
    )


@pytest.mark.asyncio
async def test_retryable_prologue_crash_goes_through_the_normal_retry(worker, monkeypatch):
    pool = _Pool()
    seen: list[int] = []

    async def _boom(pool, bot, row):
        raise RuntimeError("FLOOD_WAIT_60")

    async def _requeue(pool, op_id, exc, params, op_type, **kw):
        seen.append(op_id)
        return True

    monkeypatch.setattr(worker, "_run_op_task", _boom)
    monkeypatch.setattr(worker, "_maybe_requeue", _requeue)

    worker._active_op_ids.add(4242)
    await worker._run_op_task_guarded(pool, _Bot(), _row())

    assert seen == [4242], "сбой пролога не прошёл через обычный механизм повтора"
    assert not [q for q, _ in pool.executed if "status='failed'" in q], (
        "операция поставлена на повтор и тут же помечена проваленной"
    )
    assert 4242 not in worker._active_op_ids


@pytest.mark.asyncio
async def test_cancellation_is_reraised_and_registry_released(worker, monkeypatch):
    async def _cancelled(pool, bot, row):
        raise asyncio.CancelledError()

    monkeypatch.setattr(worker, "_run_op_task", _cancelled)

    worker._active_op_ids.add(4242)
    with pytest.raises(asyncio.CancelledError):
        await worker._run_op_task_guarded(_Pool(), _Bot(), _row())

    assert 4242 not in worker._active_op_ids, (
        "остановка процесса оставила операцию в реестре активных"
    )


@pytest.mark.asyncio
async def test_normal_run_also_releases_the_registry(worker, monkeypatch):
    async def _ok(pool, bot, row):
        return None

    monkeypatch.setattr(worker, "_run_op_task", _ok)

    worker._active_op_ids.add(4242)
    await worker._run_op_task_guarded(_Pool(), _Bot(), _row())

    assert 4242 not in worker._active_op_ids


@pytest.mark.asyncio
async def test_failure_of_the_failure_write_still_releases_the_registry(worker, monkeypatch):
    """Даже если разбор сбоя сам упал, реестр обязан освободиться."""

    async def _boom(pool, bot, row):
        raise RuntimeError("первичный сбой")

    async def _requeue_boom(*a, **kw):
        raise RuntimeError("и база недоступна")

    monkeypatch.setattr(worker, "_run_op_task", _boom)
    monkeypatch.setattr(worker, "_maybe_requeue", _requeue_boom)

    worker._active_op_ids.add(4242)
    await worker._run_op_task_guarded(_Pool(), _Bot(), _row())

    assert 4242 not in worker._active_op_ids
