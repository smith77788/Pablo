"""Долгая рассылка держала весь флот, и соседняя операция падала «флот занят».

ЧТО БЫЛО. Разовая рассылка ЛС (`_exec_bulk_dm_adhoc`) захватывает флот целиком
— `try_claim_accounts` берёт всё, что дали, — и держит его часами: темп между
сообщениями измеряется десятками секунд на аккаунт. Другая операция того же
владельца, стартовавшая позже, не находит ни одного свободного аккаунта,
крутится в живой очереди и через `_ACCT_WAIT_MAX_MIN` (20 минут) закрывается
словами «все аккаунты 20 мин были заняты другими операциями».

Механизм против этого в продукте уже есть и проверен на живой БД
(`_rebalance_claim` + tests/test_fleet_rebalance_cooperative.py): держатель
пересчитывает свою долю посреди прогона и отдаёт лишнее, учитывая и тех, кто
ЖДЁТ флот. Вызов стоял ровно в одном исполнителе — массовом инвайте.

ЧТО ПРОВЕРЯЕМ. Рассылка тоже делится: вызов встроен в её цикл, троттлится по
времени и стоит ДО выбора аккаунта для получателя (иначе отданный аккаунт ещё
успел бы отправить сообщение). Период дележа — одно число на всех
исполнителей: два независимых разошлись бы молча.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


def _executor(name: str) -> ast.AsyncFunctionDef:
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} не найден — ориентир сменился")


@pytest.mark.parametrize("name", ["_exec_bulk_dm_adhoc", "_exec_mass_invite"])
def test_a_long_holder_shares_the_fleet_on_the_fly(name):
    fn = _executor(name)
    src = ast.unparse(fn)
    assert "_rebalance_claim(" in src, (
        f"{name} держит флот до конца прогона: соседняя операция владельца "
        "не получит ни одного аккаунта и закроется «все аккаунты заняты»")
    assert "_REBALANCE_EVERY_S" in src and "time.monotonic()" in src, (
        f"{name} пересчитывает долю без троттлинга — это COUNT(*) по очереди "
        "владельца на каждую цель")


def test_the_share_is_recomputed_before_the_account_is_picked():
    """Иначе отданный аккаунт успеет отправить ещё одно сообщение."""
    fn = _executor("_exec_bulk_dm_adhoc")
    rebalance = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("_rebalance_claim")]
    picks = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("_pick_acc")]
    assert rebalance and picks, "ориентиры сменились — поправьте проверку"
    assert min(rebalance) < min(picks), (
        "дележ стоит после выбора аккаунта: получатель уйдёт через аккаунт, "
        "который уже отдан другой операции")


def test_the_period_has_one_source_of_truth():
    """Локальная копия периода разошлась бы с остальными молча."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    at_module = {t.id for node in tree.body if isinstance(node, ast.Assign)
                 for t in node.targets if isinstance(t, ast.Name)}
    assert "_REBALANCE_EVERY_S" in at_module, (
        "период дележа объявлен не на уровне модуля — его не видят соседние "
        "исполнители")
    inside: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_REBALANCE_EVERY_S"
                            for t in node.targets)):
                inside.append((fn.name, node.lineno))
    assert not inside, f"период переопределён внутри функций: {inside}"


# ── Сам дележ: отдаём долю тому, кто ЖДЁТ флот ───────────────────────────────

class _Pool:
    def __init__(self, waiting: int):
        self.waiting = waiting

    async def fetchval(self, query, *args):
        if "acct_wait_since IS NOT NULL" in query:
            return self.waiting
        return 0


@pytest.mark.asyncio
async def test_a_waiting_operation_gets_its_share(monkeypatch):
    from services import op_worker

    released: list[list[int]] = []

    async def _release(ids):
        released.append(sorted(int(i) for i in ids))

    monkeypatch.setattr(op_worker, "_db_pool", _Pool(waiting=1))
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    held = [{"id": i} for i in (1, 2, 3, 4)]

    kept = await op_worker._rebalance_claim(555, 77, held)

    assert len(kept) == 2, (
        f"отдали не половину флота при одном ждущем, а {4 - len(kept)}: "
        "ждущая операция так и не стартует")
    assert released == [[3, 4]], "отданные аккаунты не вернулись в пул"


@pytest.mark.asyncio
async def test_nobody_waiting_means_nothing_is_given_back(monkeypatch):
    """Самопроверка: в одиночку операция работает всем флотом."""
    from services import op_worker

    monkeypatch.setattr(op_worker, "_db_pool", _Pool(waiting=0))
    held = [{"id": i} for i in (1, 2, 3, 4)]
    assert await op_worker._rebalance_claim(555, 77, held) is held
