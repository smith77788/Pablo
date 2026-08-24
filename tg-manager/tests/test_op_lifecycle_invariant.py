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
    # снимает аренду в БД (через _db_release — он же гарантирует, что чужую
    # живую аренду мы не трогаем) и чистит in-memory реестр по op_id
    assert "_db_release(" in seg
    assert "_operation_account_locks.pop(op_id" in seg
    assert "_accounts_in_use.discard" in seg
    # сам _db_release обязан снимать флаг и скоупиться владельцем аренды
    rel = ow[ow.index("async def _db_release"):ow.index("async def _db_release") + 900]
    assert "in_operation   = FALSE" in rel
    assert "op_lease_owner = $2" in rel, "освобождение должно скоупиться своей репликой"


def test_claim_registers_accounts_per_op():
    """Аккаунты, взятые исполнителем, регистрируются под op_id — иначе finally
    их не освободит."""
    ow = _read("services/op_worker.py")
    seg = ow[ow.index("async def _claim_available_accounts"):
             ow.index("async def _claim_available_accounts") + 2400]
    assert "_operation_account_locks.setdefault(op_id" in seg


def test_stale_reset_on_startup_exists():
    """Страховка от жёсткого kill процесса: сброс залипших in_operation на старте.

    Сброс обязан быть ИЗБИРАТЕЛЬНЫМ: освобождать протухшие/бесхозные/свои аренды,
    но НЕ живые аренды другой реплики — иначе рестарт одного контейнера снимал бы
    защиту с сессий, которые прямо сейчас держит другой (AUTH_KEY_DUPLICATED).
    """
    ow = _read("services/op_worker.py")
    i = ow.index("async def reset_stale_in_operation")
    seg = ow[i:i + 1600]
    assert "in_operation   = FALSE" in seg
    assert "op_lease_until < now()" in seg, "протухшая аренда должна освобождаться"
    assert "op_lease_owner = $1" in seg, "своя аренда должна освобождаться"
    assert "WHERE in_operation = TRUE\n                  AND (" in seg, (
        "безусловный сброс всех флагов освобождал бы живые аренды соседних реплик"
    )
