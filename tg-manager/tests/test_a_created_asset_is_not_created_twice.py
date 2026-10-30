"""Созданный канал (или бот) не должен создаваться вторым.

Global Presence создаёт канал или бота, а потом ОТМЕЧАЕТ в цели его id
(`result_asset_id`). Отметка — не отчётность, а дверь: расклинивание плана
(`_revive_wedged_gp_targets`) разбирает брошенные цели ровно по ней и сам
объясняет ставку:

  * пусто → «ресурс создать не успели, работа не начиналась» → цель
    возвращается в очередь, и повтор создаёт канал ЗАНОВО;
  * заполнено → «канал/бот в Telegram УЖЕ СОЗДАН» → цель закрывается как
    выполненная.

Записывалась эта отметка одним `_safe_execute`, результат которого никто не
смотрел. А `_safe_execute` не бросает исключений — он возвращает 'ERROR'.
То есть один блип базы между созданием канала и отметкой приводил к тому, что
расклинивание видело пустое поле, считало, что работа не начиналась, и
аккаунт создавал ВТОРОЙ такой же канал. Для бота цена выше: лимит BotFather
считается на аккаунт, и дубль выжигает дефицитный слот.

Отметить ДО создания нельзя — id появляется только вместе с ресурсом. Поэтому
дверь держится иначе: запись повторяется, а при полной неудаче цель закрывается
на месте с честным error_message. Закрытая цель расклиниванию не попадается, и
второго канала не будет; ресурс при этом не теряется — его подбирает
сканирование собственных ресурсов, ровно как написано в расклинивании про
оборвавшийся прогон.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


def _source() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def _fn(name: str) -> ast.AST:
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"в services/op_worker.py нет функции {name}")


class _Pool:
    """Стенд базы: роняет первые `fail_marks` записей отметки."""

    def __init__(self, fail_marks: int = 0):
        self.fail_marks = int(fail_marks)
        self.marks = 0
        self.closes: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        if "result_asset_id=$1" in sql:
            self.marks += 1
            if self.marks <= self.fail_marks:
                raise RuntimeError("стенд: отметка отклонена")
            return "UPDATE 1"
        self.closes.append((sql, args))
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def _instant(monkeypatch):
    """Пауза между попытками не должна удлинять прогон тестов."""
    async def _nap(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nap)


def test_one_blip_does_not_cost_a_channel():
    """Один сбой базы — отметка ложится со второй попытки."""
    from services import op_worker

    pool = _Pool(fail_marks=1)
    assert _run(op_worker._mark_gp_asset(pool, 11, 777000, "channel")) is True
    assert pool.marks == 2, f"попыток записи: {pool.marks}"
    assert pool.closes == [], (
        "цель закрыта, хотя отметка в итоге записалась")


def test_a_lost_mark_closes_the_target_instead_of_requeueing_it():
    """База не приняла отметку — цель закрывается, а не уходит на повтор."""
    from services import op_worker

    before = _total("infragram_gp_asset_mark_write_failures_total")
    pool = _Pool(fail_marks=99)
    assert _run(op_worker._mark_gp_asset(pool, 12, 777000, "channel")) is False
    assert pool.marks >= 3, f"отметка не повторялась: попыток {pool.marks}"
    assert pool.closes, (
        "цель осталась в 'running' с пустым result_asset_id — расклинивание "
        "вернёт её в очередь и аккаунт создаст ВТОРОЙ такой же канал")
    sql = pool.closes[-1][0]
    assert "status='running'" in sql, (
        "закрытие не ограничено статусом 'running': оно затрёт исход, который "
        f"цель уже успела получить — {sql!r}")
    assert "error_message" in sql, (
        "цель закрыта молча: владелец не узнает, что оформление могло не "
        "примениться")
    assert _total("infragram_gp_asset_mark_write_failures_total") > before, (
        "счётчик защитного отказа не вырос — алерт владельцу не уйдёт")


def test_the_counter_is_in_the_protective_set():
    """Защитный отказ без счётчика и описания — молчаливый отказ."""
    from services import metrics, op_worker

    name = "infragram_gp_asset_mark_write_failures_total"
    assert name in op_worker._PROTECTIVE_FAILURE_COUNTERS, (
        f"{name} не в наборе защитных отказов")
    assert name in metrics._HELP, f"{name} без описания в services/metrics.py"


@pytest.mark.parametrize("executor", ["_exec_global_presence_channel",
                                      "_exec_global_presence_bot"])
def test_the_mark_goes_through_the_door(executor):
    """Храповик: оба исполнителя отмечают актив через дверь."""
    body = ast.unparse(_fn(executor))
    assert "_mark_gp_asset(" in body, (
        f"{executor} пишет result_asset_id не через _mark_gp_asset: результат "
        "записи снова некому проверить")
    bare = [line for line in body.splitlines()
            if "result_asset_id=$1 WHERE id=$2" in line]
    assert bare == [], (
        f"{executor} всё ещё пишет отметку напрямую: {bare}")


def test_the_stake_is_still_the_stake():
    """Самопроверка: расклинивание по-прежнему судит цель по этой отметке.

    Если разбор перестанет возвращать в очередь цели с пустым полем, причина
    этой двери изменится — и тогда её надо переписать, а не держать как есть.
    """
    body = ast.unparse(_fn("_revive_abandoned_gp_targets"))
    assert "result_asset_id IS NULL" in body and "status='pending'" in body, (
        "расклинивание больше не возвращает в очередь цели с пустой отметкой — "
        "перечитайте причину двери в докстринге этого модуля")
