"""Прод-инвариант (Этап 5, «падения операций»): при КРАШЕ операции аккаунты
обязаны освобождаться (in_operation=FALSE), слот параллельности — освобождаться,
статус операции — становиться 'failed'. Иначе аккаунты застревают «занятыми» до
рестарта и все дальнейшие операции с ними молча пропускаются.

Инвариант держится несколькими механизмами; тест сторожит их от регресса при
рефакторинге (op_worker импортирует telethon → проверяем исходником)."""
from __future__ import annotations
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_run_op_task_releases_on_crash():
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    # except-ветка помечает операцию failed
    assert "UPDATE operation_queue SET status='failed'" in body
    # finally ОБЯЗАН освобождать аккаунты и слот параллельности
    fin = body[body.rindex("finally:"):]
    assert "release_operation_accounts(op_id)" in fin
    assert "_active_op_ids.discard(op_id)" in fin


def test_release_operation_accounts_resets_flag():
    ow = _read("services/op_worker.py")
    seg = ow[ow.index("async def release_operation_accounts"):
             ow.index("async def release_operation_accounts") + 900]
    # снимает in_operation в БД и чистит in-memory реестр по op_id
    assert "in_operation=FALSE" in seg
    assert "_operation_account_locks.pop(op_id" in seg
    assert "_accounts_in_use.discard" in seg


def test_claim_registers_accounts_per_op():
    """Аккаунты, взятые исполнителем, регистрируются под op_id — иначе finally
    их не освободит."""
    ow = _read("services/op_worker.py")
    seg = ow[ow.index("async def _claim_available_accounts"):
             ow.index("async def _claim_available_accounts") + 2400]
    assert "_operation_account_locks.setdefault(op_id" in seg


def test_stale_reset_on_startup_exists():
    """Страховка от жёсткого kill процесса: сброс залипших in_operation на старте."""
    ow = _read("services/op_worker.py")
    assert "async def reset_stale_in_operation" in ow
    assert "SET in_operation = FALSE WHERE in_operation = TRUE" in ow
