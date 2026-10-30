"""Сетевая рассылка не должна завести по одному боту две рассылки.

Сетевая рассылка идёт по списку ботов и на каждого заводит ОТДЕЛЬНУЮ рассылку
со своим id. Журнал доставок ведётся по id рассылки, поэтому вторая рассылка по
тому же боту — это вся его аудитория, получившая сообщение второй раз.

Единственное, что отличает повторный прогон операции от первого, — отметка
`launched_bot_ids` в params. Повторы здесь не редкость, а норма: рабочая ветка
едет в прод на каждом пуше, рестарт возвращает операцию в очередь и обнуляет
`done_items`, и исполнитель идёт по списку ботов с начала.

Дверь стояла, но была открыта с двух сторон:

  * отметка писалась ПОСЛЕ `broadcaster.start` — в зазоре между запуском и
    записью падение процесса стоило одной лишней рассылки на всю аудиторию;
  * результат записи выбрасывался на пол (`_safe_execute` отдаёт 'ERROR', и
    его никто не смотрел) — блип базы стоил того же.

Теперь отметка — цена запуска: сначала запись (с повтором), и только потом
`broadcaster.start`. Потерять запуск эта перестановка не может: строка рассылки
уже создана и стоит в 'pending', а ровно эти статусы поднимает
`resume_interrupted` на следующем старте процесса. «Отметили, но не успели
запустить» догоняется само; «запустили, но не отметили» не лечится ничем.

И отказ больше не молчит: пропущенные боты считаются и попадают в итог, иначе
операция закрывалась словами «нет пользователей в сегменте» на сегменте, где
пользователи были.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


def _nb_launch_body() -> tuple[ast.AST, str]:
    """Тело `_nb_launch` деревом и текстом."""
    path = os.path.join(ROOT, "services", "op_worker.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_nb_launch":
            return node, ast.get_source_segment(src, node) or ""
    raise AssertionError("в services/op_worker.py не нашлась функция _nb_launch")


class _Pool:
    """Стенд базы: по требованию роняет ИМЕННО отметку о запуске бота."""

    def __init__(self, fail_marks: int = 0):
        self.fail_marks = int(fail_marks)
        self.marks = 0

    async def execute(self, sql, *args):
        if "params = COALESCE(params" in sql:
            self.marks += 1
            if self.marks <= self.fail_marks:
                raise RuntimeError("стенд: отметка отклонена")
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        if "FROM bot_users" in sql:
            return [{"user_id": 1001}, {"user_id": 1002}]
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None


class _Bot:
    pass


_BOTS = [
    {"bot_id": 701, "token": "701:AAH-stand"},
    {"bot_id": 702, "token": "702:AAH-stand"},
]


@pytest.fixture
def stand(monkeypatch):
    """Подменяем всё, что исполнитель зовёт вокруг отметки."""
    from database import db
    from services import broadcaster, op_worker

    created: list[int] = []
    started: list[int] = []
    abandoned: list[int] = []
    _next = [5100]

    async def _get_bots(pool, owner_id):
        return list(_BOTS)

    async def _create(pool, bot_id, text, total, owner_id, **kw):
        _next[0] += 1
        created.append(_next[0])
        return _next[0]

    def _start(*a, **kw):
        started.append(int(a[2]))

    async def _abandon(pool, broadcast_id):
        abandoned.append(int(broadcast_id))

    async def _await_nb(pool, op_id, bc_ids, total_users):
        return {"status": "done", "ok": total_users, "summary": "стенд: дождались"}

    async def _not_cancelled(pool, op_id):
        return False

    monkeypatch.setattr(db, "get_bots", _get_bots)
    monkeypatch.setattr(db, "create_broadcast", _create)
    monkeypatch.setattr(broadcaster, "start", _start)
    monkeypatch.setattr(broadcaster, "abandon_unstarted", _abandon)
    monkeypatch.setattr(op_worker, "_await_network_broadcasts", _await_nb)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    return {"created": created, "started": started, "abandoned": abandoned}


def test_an_unmarked_bot_is_not_launched(stand):
    """База не приняла отметку — рассылка по этому боту не уходит."""
    from services import op_worker

    before = _total("infragram_network_broadcast_mark_write_failures_total")
    pool = _Pool(fail_marks=99)
    res = _run(op_worker._exec_network_broadcast(
        pool, _Bot(), 910, 55, {"text": "Привет", "segment": "all_each"}))

    assert stand["started"] == [], (
        "рассылка ушла, а бот не закреплён за операцией: повтор прогона заведёт "
        "по нему вторую рассылку и отправит её всей аудитории заново")
    assert stand["abandoned"] == stand["created"], (
        "незапущенные строки рассылок не сняты: ровно статусы "
        "('pending','running') поднимает resume_interrupted на следующем "
        "старте процесса, и они уедут мимо операции")
    assert _total("infragram_network_broadcast_mark_write_failures_total") > before, (
        "счётчик защитного отказа не вырос — владельцу об этом никто не скажет")


def test_a_failed_mark_is_retried_before_giving_up(stand):
    """Один блип базы не стоит рассылки: отметка пишется со второй попытки."""
    from services import op_worker

    pool = _Pool(fail_marks=1)
    _run(op_worker._exec_network_broadcast(
        pool, _Bot(), 911, 55, {"text": "Привет", "segment": "all_each"}))

    assert pool.marks >= 3, (
        f"отметка не повторялась после сбоя (попыток записи: {pool.marks})")
    assert len(stand["started"]) == len(_BOTS), (
        "после удачного повтора отметки рассылка обязана уйти: "
        f"запущено {len(stand['started'])} из {len(_BOTS)}")


def test_a_refusal_is_not_reported_as_an_empty_segment(stand):
    """Итог не смеет называть отказ отсутствием пользователей."""
    from services import op_worker

    res = _run(op_worker._exec_network_broadcast(
        _Pool(fail_marks=99), _Bot(), 912, 55,
        {"text": "Привет", "segment": "all_each"}))

    assert res.get("status") != "done", (
        f"операция закрыта успехом, не отправив ничего: {res}")
    assert "Нет пользователей" not in (res.get("summary") or ""), (
        "владельцу сказали «нет пользователей в сегменте» на сегменте, где "
        f"они были: {res.get('summary')!r}")


def test_a_partly_refused_run_is_not_reported_as_done(stand):
    """Часть ботов пропущена — это 'partial', а не 'готово'."""
    from services import op_worker

    # Роняем отметку только второго бота: первый уходит, второй пропущен.
    pool = _Pool()
    _fail_after = [1]

    async def _execute(sql, *args):
        if "params = COALESCE(params" in sql:
            pool.marks += 1
            if pool.marks > _fail_after[0]:
                raise RuntimeError("стенд: отметка отклонена")
        return "UPDATE 1"

    pool.execute = _execute  # type: ignore[method-assign]
    res = _run(op_worker._exec_network_broadcast(
        pool, _Bot(), 913, 55, {"text": "Привет", "segment": "all_each"}))

    assert len(stand["started"]) == 1, stand["started"]
    assert res.get("status") == "partial", (
        f"половина работы не сделана, а операция закрыта как {res.get('status')!r}: {res}")
    assert "ропущен" in (res.get("summary") or ""), (
        f"в итоге нет ни слова о пропущенных ботах: {res.get('summary')!r}")


def test_a_bot_launched_before_is_not_launched_again(stand):
    """Повторный прогон читает отметку и второй рассылки не заводит."""
    from services import op_worker

    pool = _Pool()
    res = _run(op_worker._exec_network_broadcast(
        pool, _Bot(), 914, 55,
        {"text": "Привет", "segment": "all_each",
         "launched_bot_ids": [701, 702], "launched_bc_ids": [5101, 5102]}))

    assert stand["created"] == [], (
        "повторный прогон завёл новые строки рассылок по уже запущенным ботам: "
        "их аудитория получит сообщение второй раз")
    assert stand["started"] == [], stand["started"]
    assert res.get("status") == "done", res


def test_the_mark_is_written_before_the_launch():
    """Храповик: отметка стоит в коде ДО запуска, а не после."""
    node, text = _nb_launch_body()
    mark = text.find("params = COALESCE(params")
    launch = text.find("broadcaster.start(")
    assert mark != -1, "в _nb_launch нет записи отметки launched_bot_ids"
    assert launch != -1, "в _nb_launch нет вызова broadcaster.start"
    assert mark < launch, (
        "отметка о запуске пишется ПОСЛЕ broadcaster.start: падение процесса в "
        "зазоре между ними стоит одной лишней рассылки на всю аудиторию бота")


def test_the_mark_write_result_is_not_dropped_on_the_floor():
    """Храповик: результат записи отметки проверяется, а не выбрасывается."""
    node, text = _nb_launch_body()
    head = text[:text.find("broadcaster.start(")]
    assert "'ERROR'" in head or '"ERROR"' in head, (
        "результат записи отметки не сверяется с 'ERROR': _safe_execute не "
        "бросает исключений, поэтому незамеченный сбой базы запускает "
        "рассылку незакреплённой")


def test_the_counter_is_in_the_protective_set():
    """Защитный отказ без счётчика и алерта — молчаливый отказ."""
    from services import metrics, op_worker

    name = "infragram_network_broadcast_mark_write_failures_total"
    assert name in op_worker._PROTECTIVE_FAILURE_COUNTERS, (
        f"{name} не в наборе защитных отказов: владелец не узнает, что "
        "рассылка не ушла")
    assert name in metrics._HELP, f"{name} без описания в services/metrics.py"
