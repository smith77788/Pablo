"""Read-only проверка ограничений не забирает весь флот под себя.

ЧТО БЫЛО (жалоба владельца, скриншоты). «Проверка каналов/чатов/ботов на
ограничения» захватывала ВЕСЬ флот (_claim_available_accounts) и держала его всю
свою длительность — на крупном флоте это 20–30 минут (301 сущность). При этом
read-only проверке реально нужны лишь несколько наблюдателей (управляющий
сущностью + один внешний на проверку видимости). Пока она шла:
  * Strike брал ОДИН аккаунт из всего флота («Аккаунтов: 1»);
  * «Проверка статуса всех аккаунтов» 21 минуту не могла начать и падала
    «флот был занят другими операциями».

ЧТО СТАЛО. Проверка захватывает аккаунты ЛЕНИВО — только те, что реально
подключает (claim_account_for_op в _client_for), а остальной флот остаётся
свободным для действий. Арбитр тот же (try_claim_accounts), поэтому одна сессия
по-прежнему не уходит двум операциям (защита от AUTH_KEY_DUPLICATED), а
освобождение — той же дверью (release_operation_accounts по op_id).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services import op_worker

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _executor_src(name: str) -> str:
    for fn in ast.walk(ast.parse(SRC)):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == name:
            return ast.unparse(fn)
    raise AssertionError(f"{name} не найден — ориентир сменился")


def test_restriction_check_claims_lazily_not_the_whole_fleet():
    src = _executor_src("_exec_check_owned_restrictions")
    # Весь флот заранее больше НЕ захватываем.
    assert "_claim_available_accounts" not in src, (
        "проверка снова забирает весь флот заранее — strike/жалобы будут "
        "голодать и брать один аккаунт")
    # Захват ленивый, под операцию, через общий арбитр.
    assert "claim_account_for_op(op_id" in src, (
        "наблюдатель должен захватываться лениво (claim_account_for_op), а не "
        "весь флот разом")


@pytest.fixture
def _clean_fleet_state():
    """Изолируем модульные множества захвата от других тестов."""
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()
    yield
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()


async def test_lazy_claim_binds_to_op_and_blocks_double_use(_clean_fleet_state):
    op1, op2, acc = 90101, 90102, 555

    # Ленивый захват под операцию op1.
    assert await op_worker.claim_account_for_op(op1, acc) is True
    assert acc in op_worker._accounts_in_use
    assert acc in op_worker._operation_account_locks.get(op1, set())

    # Пока op1 держит сессию — НИКТО другой её не получит (нет двух клиентов на
    # одном auth-key). Это и есть защита от AUTH_KEY_DUPLICATED.
    assert await op_worker.claim_account_for_op(op2, acc) is False
    assert await op_worker.try_claim_account(acc) is False

    # Освобождение той же дверью, что у пакетного захвата, — по op_id.
    await op_worker.release_operation_accounts(op1)
    assert acc not in op_worker._accounts_in_use

    # После освобождения аккаунт снова свободен для действия.
    assert await op_worker.claim_account_for_op(op2, acc) is True


async def test_lazy_claim_leaves_other_accounts_free(_clean_fleet_state):
    """Захватили одного наблюдателя — остальной флот свободен для strike/жалоб."""
    check_op = 90110
    fleet = [11, 12, 13, 14, 15]
    # Проверка подключила лишь двух наблюдателей.
    assert await op_worker.claim_account_for_op(check_op, 11) is True
    assert await op_worker.claim_account_for_op(check_op, 12) is True
    # Остальные три — свободны: действие владельца их получит.
    free = [a for a in fleet if a not in op_worker._accounts_in_use]
    assert free == [13, 14, 15]
