"""Отмена операции идёт одной дверью — как и постановка.

МЕХАНИКА. `operation_bus.cancel` называет себя ЕДИНСТВЕННОЙ ДВЕРЬЮ ОТМЕНЫ и
перечисляет в докстринге, чем расходились пять сырых UPDATE, которые она
заменила: три не ставили `finished_at`, одна не фильтровала статус и могла
отменить завершённую, и ни одна не дописывала `result` и не объявляла исход —
поэтому отмена не попадала ни на график исходов, ни в подписанный аудит-трейл,
ни в память организма.

ЧТО БЫЛО НЕ ТАК. Две поверхности консолидацию не прошли, и вторую нашла уже
сама перепись — ручной grep по одной строке её не видел, потому что SQL там
многострочный: кнопка «Пауза» в кампаниях рассылки (`cb_dm_pause`) гасила
операцию своим UPDATE, выбирая её по параметрам кампании. Первая:
`cancel_global_presence_plan` гасила связанную операцию своим UPDATE внутри
транзакции плана. Статус она фильтровала аккуратно (терминальный не
переписывала), но `result` не писала и исход не объявляла — то есть ровно
четыре из пяти перечисленных дефектов возвращались на этом пути. Операция
оставалась терминальной и ПУСТОЙ: владелец видит «Отменено» без единой цифры,
хотя часть целей отработана, и именно по этой цифре решает, продолжать ли.

Сторож сирот отмены такую операцию подбирает — но он написан под гонку с
SIGTERM, а не под предсказуемый обход двери, и подбирает её с задержкой
`_CANCEL_ORPHAN_GRACE_MIN`.

Обе чинились одинаково — вызовом двери, — но по-разному в деталях: плану нужен
расширенный `allow` (он гасил операцию из любого нетерминального состояния), а
«Паузе» — предварительная ВЫБОРКА операции, потому что дверь принимает op_id, а
поверхность знает только кампанию.

Парная перепись к `test_an_operation_is_queued_only_through_the_bus`: там
INSERT, здесь отмена.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Дверь отмены живёт здесь — ей сырой UPDATE и положен.
CANCEL_DOOR_FILE = os.path.join("services", "operation_bus.py")

# Дозакрытие уже отменённой строки: дописывает цифры тому, что дверь закрыла.
# Это не отмена, а её вторая половина, и живёт она в воркере.
ENRICHERS = {
    os.path.join("services", "op_worker.py"): "_finish_cancelled_op дописывает "
    "итог строке, которую дверь уже перевела в 'cancelled'",
}

# Осознанные обходы — с причиной. Пустой набор значит «обходов нет».
CANCEL_BYPASS_ON_PURPOSE: dict[str, str] = {}


def _py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("tests", "__pycache__", ".git", "node_modules",
                                "venv", ".venv", "migrations")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def _raw_cancels():
    """Сырые UPDATE, переводящие операцию в 'cancelled'."""
    out = []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT)
        if rel == CANCEL_DOOR_FILE or rel in ENRICHERS or rel in CANCEL_BYPASS_ON_PURPOSE:
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if "operation_queue" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value
            if not re.search(r"UPDATE\s+operation_queue", sql, re.I):
                continue
            if not re.search(r"SET[^\n]*status\s*=\s*'cancelled'", sql, re.I | re.S):
                continue
            out.append((rel, node.lineno))
    return out


def test_the_census_finds_the_door_itself():
    """Самопроверка: убери дверь из исключений — и она найдётся."""
    saved = os.path.join("services", "operation_bus.py")
    with open(os.path.join(ROOT, saved), encoding="utf-8") as fh:
        src = fh.read()
    assert re.search(r"SET\s+status\s*=\s*'cancelled'", src, re.I), (
        "перепись ищет текст, которого в самой двери больше нет — "
        "отбор сломан, а не код чист")


def test_no_surface_cancels_an_operation_by_itself():
    bad = _raw_cancels()
    assert not bad, (
        "отмена операции идёт мимо `operation_bus.cancel`: "
        f"{bad}. Своя отмена не пишет `result` и не объявляет исход — "
        "владелец увидит «Отменено» без единой цифры, а на графике исходов "
        "и в аудит-трейле отмены не будет вовсе")


def test_the_plan_cancel_preserves_the_statuses_it_used_to_reach():
    """Дверь по умолчанию отменяет из двух состояний, а сырой UPDATE — из всех.

    `CANCELLABLE` — это ('pending','running'). Сырой UPDATE гасил операцию из
    любого НЕтерминального состояния, то есть и из 'paused', 'scheduled',
    'waiting_approval'. Перевести путь на дверь, не расширив `allow`, значило
    бы оставить приостановленную операцию живой после отмены её плана — плана
    нет, операция идёт.
    """
    from services import op_status

    with open(os.path.join(ROOT, "database", "db.py"), encoding="utf-8") as fh:
        src = fh.read()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "cancel_global_presence_plan")
    body = "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    assert "operation_bus" in body, (
        "отмена плана больше не ходит через шину — см. перепись выше")
    assert "IN_FLIGHT" in body, (
        "путь переведён на дверь, но `allow` не расширен до нетерминальных "
        f"состояний ({sorted(op_status.IN_FLIGHT)}): приостановленная "
        "операция переживёт отмену своего плана")


@pytest.mark.asyncio
async def test_cancelling_a_plan_closes_its_operation_through_the_door(monkeypatch):
    """Функционально: отмена плана зовёт дверь, а не пишет статус сама."""
    from database import db
    from services import operation_bus

    calls = []

    async def _fake_cancel(pool, op_id, owner_id, allow=None):
        calls.append({"op_id": op_id, "owner_id": owner_id,
                      "allow": tuple(allow or ())})
        return True

    monkeypatch.setattr(operation_bus, "cancel", _fake_cancel)

    executed = []

    class _Conn:
        async def fetchrow(self, sql, *a):
            return {"op_id": 777}

        async def execute(self, sql, *a):
            executed.append(sql)
            return "UPDATE 1"

        def transaction(self):
            return _Tx()

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

        async def fetchrow(self, sql, *a):
            return {"op_id": 777}

    ok = await db.cancel_global_presence_plan(_Pool(), 5, 42)
    assert ok is True

    assert calls, "дверь отмены не вызвана — операция гасится в обход шины"
    assert calls[0]["op_id"] == 777
    assert calls[0]["owner_id"] == 42
    assert "paused" in calls[0]["allow"], (
        "в `allow` нет 'paused' — приостановленная операция переживёт "
        "отмену своего плана")

    for sql in executed:
        assert not re.search(r"UPDATE\s+operation_queue", sql, re.I), (
            f"операция всё ещё гасится своим UPDATE: {sql!r}")
