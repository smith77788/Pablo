"""Операция не должна стоять в 'running' без прогресса, ожидая местный семафор.

Механика. Авторитетный потолок параллельности на владельца стоит в SQL поллера:
`owner_running` считает `status='running'` по ВСЕЙ таблице и пропускает в окно
только те ожидающие, у которых `running_count + owner_pending_rank <= лимит`.
Этот гейт считает все процессы, а не свой, и перенесли его в SQL именно потому,
что раньше лишние задачи владельца получали `status='running'`, а фактически
стояли внутри семафора и выглядели для владельца зависшими (см. комментарий в
`_process_pending`).

Сам семафор при этом остался как местная подстраховка — и остался БЛОКИРУЮЩИМ.
А блокирует он уже ПОСЛЕ того, как поллер поставил операции 'running' и записал
её в `_active_op_ids`. Пока она там стоит:

  * владелец видит «выполняется» с нулевым прогрессом;
  * операция держит один из восьми общих слотов;
  * сторож зависших её НЕ сбрасывает — ровно потому, что она в `_active_op_ids`.

То есть ожидание здесь воспроизводит тот самый симптом, от которого избавлялись.
Семафор не может пропустить ЛИШНЕЕ (он только блокирует), поэтому убирать
подстраховку не нужно — нужно перестать ждать её бесконечно.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        return f.read()


def _fn(name: str) -> str:
    src = _src()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == name)
    return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])


# ── Захват ограничен по времени ──────────────────────────────────────────────

def test_acquire_has_a_ceiling():
    body = _fn("_run_op_task")
    assert "wait_for(owner_sem.acquire()" in body, (
        "захват семафора снова бессрочный — операция будет стоять в 'running'")
    assert "async with owner_sem:" not in body, (
        "остался блокирующий `async with owner_sem` — он и есть бессрочное ожидание")


def test_timeout_returns_the_operation_to_the_queue():
    """Выйти из задачи, не вернув операцию в очередь, значит оставить её на час."""
    body = _fn("_run_op_task")
    seg = body[body.index("wait_for(owner_sem.acquire()"):]
    seg = seg[:seg.index("async with _releasing")]
    assert "_release_op_for_owner_limit" in seg, (
        "по таймауту операция не возвращается в очередь — повиснет в 'running' "
        "до сторожа зависших, то есть на час")
    assert "_active_op_ids.discard" in seg, (
        "операция не выписана из активных — поллер не возьмёт её снова")


def test_requeue_keeps_the_queue_invariants():
    body = _fn("_release_op_for_owner_limit")
    assert "status='pending'" in body and "started_at=NULL" in body
    assert "done_items=0" in body, (
        "без сброса прогресс копится поверх прошлого прогона (класс «done > total»)")
    assert "scheduled_for" in body, (
        "без отсрочки операция вернётся в тот же тик и упрётся в тот же семафор")
    assert "status NOT IN" in body, (
        "возврат в очередь обязан уважать терминальные статусы: иначе отмена "
        "владельца во время ожидания молча отменялась бы")


def test_ceiling_is_generous_enough_for_the_normal_reason():
    """Штатная занятость коротка: соседняя задача доигрывает свой finally."""
    from services import op_worker
    assert op_worker._OWNER_SEM_WAIT_S >= 10, (
        "потолок ожидания занижен: соседняя задача того же владельца не успеет "
        "доиграть finally, и операции начнут без нужды уходить в очередь")


# ── Освобождение осталось надёжным ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_releasing_frees_the_slot_even_on_error():
    """Захват с потолком не должен стоить надёжности освобождения."""
    from services import op_worker

    sem = asyncio.Semaphore(1)
    await sem.acquire()
    assert sem.locked()
    with pytest.raises(RuntimeError):
        async with op_worker._releasing(sem):
            raise RuntimeError("прогон упал")
    assert not sem.locked(), "место не освободилось — владелец потерял слот навсегда"


@pytest.mark.asyncio
async def test_releasing_frees_the_slot_on_normal_exit():
    from services import op_worker

    sem = asyncio.Semaphore(1)
    await sem.acquire()
    async with op_worker._releasing(sem):
        pass
    assert not sem.locked()


# ── Авторитетный гейт остаётся в SQL ─────────────────────────────────────────

def test_the_database_gate_is_still_the_real_limit():
    """Если гейт уйдёт из SQL, местный семафор станет единственным — и неверным.

    В памяти он считает свой процесс, поэтому на двух репликах суммарный
    потолок удвоился бы. Поэтому проверка про семафор имеет смысл только вместе
    с этой.
    """
    body = _fn("_process_pending")
    assert "owner_running" in body and "running_count + owner_pending_rank" in body, (
        "потолок на владельца больше не считается в SQL — вернулась зависимость "
        "от памяти одного процесса")
    assert re.search(r"WHERE\s+status\s*=\s*'running'", body), (
        "owner_running считает не запущенные операции — гейт стал фиктивным")
