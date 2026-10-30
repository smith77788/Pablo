"""Созданный узел связки не должен создаваться вторым.

Развёртывание связки (Фаза 2) идёт по узлам плана и создаёт недостающие
каналы, группы и ноды. Дверь, которая не даёт сделать это дважды, — `ref_id`
узла: прогон пропускает узел, у которого он уже стоит. Повтор здесь норма, а
не редкость: рабочая ветка едет в прод на каждом пуше, рестарт возвращает
операцию в очередь, и прогон идёт по узлам с начала.

Писалась эта отметка вызовом `network_builder.update_node_status`, который
исключений не бросает: он возвращает `{'ok': False, 'error': ...}` и пишет
предупреждение в лог. Результат никто не смотрел. То есть один блип базы между
созданием канала и отметкой приводил к тому, что следующий прогон видел узел
без `ref_id`, считал его незапущенным и создавал ВТОРОЙ канал с тем же
названием.

Отметить ДО создания нельзя — `ref_id` появляется только вместе с каналом.
Поэтому окно сжимается до одной атомарной записи: запись в каталог
(`managed_channels`) и отметка узла идут ОДНОЙ транзакцией, с повтором. Ровно
так уже устроен Global Presence в этом же файле.

А при полной неудаче прогон перестаёт говорить «готово»: созданный канал есть
в каталоге, и владелец видит его id и название в ручных шагах, чтобы связать
узел самому.
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


def _fn(name: str) -> ast.AST:
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"в services/op_worker.py нет функции {name}")


class _Conn:
    def __init__(self, pool):
        self._pool = pool

    def transaction(self):
        pool = self._pool

        class _Tx:
            async def __aenter__(self_inner):
                pool.tx_opened += 1
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _Tx()

    async def execute(self, sql, *args):
        if "network_nodes" in sql and "ref_id" in sql:
            self._pool.marks += 1
            if self._pool.marks <= self._pool.fail_marks:
                raise RuntimeError("стенд: отметка узла отклонена")
        return "UPDATE 1"


class _Acquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return _Conn(self._pool)

    async def __aexit__(self, *a):
        return False


class _Pool:
    """Стенд базы: роняет первые `fail_marks` отметок узла."""

    def __init__(self, fail_marks: int = 0):
        self.fail_marks = int(fail_marks)
        self.marks = 0
        self.tx_opened = 0

    def acquire(self):
        return _Acquire(self)

    async def execute(self, sql, *args):
        if "network_nodes" in sql and "ref_id" in sql:
            self.marks += 1
            if self.marks <= self.fail_marks:
                raise RuntimeError("стенд: отметка узла отклонена")
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None


_NODES = [{"id": 1, "node_type": "channel", "label": "Первый", "ref_id": None}]


@pytest.fixture
def stand(monkeypatch):
    """Подменяем всё, что исполнитель зовёт вокруг отметки узла."""
    from services import account_manager, network_builder, op_worker

    async def _detail(pool, owner_id, instance_id):
        return {"nodes": [dict(n) for n in _NODES], "edges": []}

    async def _create_channel(session, title, **kw):
        return {"channel_id": 900100, "username": None, "access_hash": 7,
                "type": "channel"}

    async def _status(pool, node_id, status, ref_id=None):
        return {"ok": True}

    async def _claim(acc_id):
        return True

    async def _release(ids):
        return None

    async def _progress(pool, op_id, done):
        return None

    async def _not_cancelled(pool, op_id):
        return False

    async def _free_tier(pool, owner_id):
        return False

    async def _sel(pool, owner_id, **kw):
        return {"id": 5, "session_str": "stand", "owner_id": 55}

    async def _nap(*a, **kw):
        return None

    from services import brand_injection, resource_selector

    monkeypatch.setattr(network_builder, "get_instance_detail", _detail)
    monkeypatch.setattr(network_builder, "update_node_status", _status)
    monkeypatch.setattr(account_manager, "create_channel", _create_channel)
    monkeypatch.setattr(resource_selector, "select_account", _sel)
    monkeypatch.setattr(brand_injection, "is_user_free_tier", _free_tier)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_mark_progress", _progress)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    monkeypatch.setattr(op_worker, "_governed_sleep", _nap)
    monkeypatch.setattr(asyncio, "sleep", _nap)
    return {}


def _deploy(pool):
    from services import op_worker

    return _run(op_worker._exec_deploy_network(
        pool, None, 950, 55, {"instance_id": 3}))


def test_one_blip_does_not_cost_a_channel(stand):
    """Один сбой базы — отметка узла ложится со второй попытки."""
    pool = _Pool(fail_marks=1)
    res = _deploy(pool)

    assert pool.marks >= 2, f"отметка не повторялась: попыток {pool.marks}"
    assert res.get("status") == "done", res
    assert res.get("created") == 1, res


def test_a_lost_mark_is_not_reported_as_done(stand):
    """База не приняла отметку — «готово» это не «готово»."""
    before = _total("infragram_network_node_mark_write_failures_total")
    pool = _Pool(fail_marks=99)
    res = _deploy(pool)

    assert res.get("status") != "done", (
        "узел остался без ref_id, а операция закрыта успехом: следующий прогон "
        f"создаст ВТОРОЙ канал с тем же названием — {res}")
    joined = " ".join(res.get("manual") or []) + " " + (res.get("summary") or "")
    assert "900100" in joined, (
        "в ручных шагах нет id созданного канала: владелец не сможет связать "
        f"узел сам — {joined!r}")
    assert _total("infragram_network_node_mark_write_failures_total") > before, (
        "счётчик защитного отказа не вырос — алерт владельцу не уйдёт")


def test_the_catalog_and_the_mark_go_in_one_transaction(stand):
    """Запись в каталог и отметка узла — одной транзакцией."""
    pool = _Pool()
    _deploy(pool)
    assert pool.tx_opened >= 1, (
        "запись в каталог и отметка узла идут двумя раздельными запросами: "
        "обрыв между ними снова оставит канал без узла")


def test_the_door_is_still_the_door():
    """Самопроверка ставки: прогон по-прежнему судит узел по ref_id."""
    body = ast.unparse(_fn("_exec_deploy_network"))
    assert "ref_id" in body and "continue" in body, (
        "прогон больше не пропускает узлы с заполненным ref_id — перечитайте "
        "причину двери в докстринге этого модуля")


def test_the_counter_is_in_the_protective_set():
    """Защитный отказ без счётчика и описания — молчаливый отказ."""
    from services import metrics, op_worker

    name = "infragram_network_node_mark_write_failures_total"
    assert name in op_worker._PROTECTIVE_FAILURE_COUNTERS, (
        f"{name} не в наборе защитных отказов")
    assert name in metrics._HELP, f"{name} без описания в services/metrics.py"
