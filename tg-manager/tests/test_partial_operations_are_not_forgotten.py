"""Недоведённая операция не теряется из повтора, эскалации и диагностики.

ЧТО БЫЛО. Исход `partial` — честное имя операции, которая взяла часть целей и
оборвалась: 203 приглашения из 380. Он появился именно для того, чтобы владелец
не читал «не запустилась» там, где работа шла. Но все три места, где продукт
сам ищет недоделанную работу, фильтровали ровно по `status='failed'`:

  * `mini_app_api._retry_failed_ops_core` — кнопка «повторить упавшие»;
  * `recovery_engine._operation_recovery` — эскалация операций, исчерпавших
    попытки;
  * `fleet_doctor.diagnose` — список «последние реальные ошибки».

То есть операции с САМЫМ БОЛЬШИМ объёмом незакрытой работы выпадали из всех
трёх: кнопка их молча не повторяла, эскалация о них не сообщала, диагностика их
причину не показывала. Чем честнее становился итог, тем тише исчезала работа.

ФИКС: один источник правды — `op_status.sql_unfinished_list()`, то есть
`('failed', 'partial')`, и все три читателя ходят через него.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import fleet_doctor, mini_app_api, op_status, recovery_engine


def _allowed_statuses(query: str) -> set[str] | None:
    """Статусы, которые запрос реально пропускает. None — фильтра нет.

    Заглушка обязана применять предикат честно: фильтр живёт в SQL, и пул,
    отдающий строки как есть, не отличил бы фикс от его отсутствия.
    """
    m = re.search(r"status\s+IN\s*\(([^)]*)\)", query)
    if m:
        return set(re.findall(r"'(\w+)'", m.group(1)))
    m = re.search(r"status\s*=\s*'(\w+)'", query)
    if m:
        return {m.group(1)}
    return None


def test_unfinished_list_covers_both_honest_outcomes():
    # Самопроверка источника правды: без 'partial' весь фикс бессмысленен.
    assert op_status.sql_unfinished_list() == "('failed', 'partial')"
    assert op_status.PARTIAL in op_status.UNFINISHED
    assert op_status.FAILED in op_status.UNFINISHED
    assert op_status.DONE not in op_status.UNFINISHED
    assert op_status.CANCELLED not in op_status.UNFINISHED


def _with_aliases(row: dict, query: str) -> dict:
    """Досчитывает алиасы вида `COALESCE(a, b) AS x`, которые читает код.

    Без этого строка приходит без ключа `op`, обращение к нему падает, и
    защитный `except` в диагностике проглатывает падение — тест был бы красным
    и с фиксом, и без него, то есть ничего не доказывал бы.
    """
    out = dict(row)
    for cols, alias in re.findall(r"COALESCE\(([^()]*)\)\s+AS\s+(\w+)", query):
        names = [c.strip() for c in cols.split(",") if "'" not in c]
        for name in names:
            if row.get(name) is not None:
                out[alias] = row[name]
                break
        else:
            out.setdefault(alias, None)
    return out


class _QueuePool:
    """Пул с одной таблицей операций, применяющий фильтр статуса по-настоящему."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.queries: list[str] = []

    def _select(self, query):
        self.queries.append(query)
        if "operation_queue" not in query:
            return []
        allowed = _allowed_statuses(query)
        rows = (list(self.rows) if allowed is None
                else [r for r in self.rows if r["status"] in allowed])
        return [_with_aliases(r, query) for r in rows]

    async def fetch(self, query, *args):
        return self._select(query)

    async def fetchrow(self, query, *args):
        rows = self._select(query)
        return rows[0] if rows else None

    async def fetchval(self, query, *args):
        row = await self.fetchrow(query, *args)
        if not row:
            return None
        return next(iter(row.values()))

    async def execute(self, query, *args):
        self.queries.append(query)
        return "UPDATE 1"


def _partial_op(op_id=4242, op_type="mass_invite"):
    return {
        "id": op_id,
        "op_type": op_type,
        "status": op_status.PARTIAL,
        "params": {"targets": ["a", "b"]},
        "label": "Приглашение в чат",
        "total_items": 380,
        "done_items": 203,
        "retry_count": 3,
        "max_retries": 3,
        "error_msg": "часть целей не взята: FloodWait",
        "last_error": "часть целей не взята: FloodWait",
        "created_at": None,
        "finished_at": None,
        "notified_at": None,
    }


def test_bulk_retry_offers_partial_operation(monkeypatch):
    """«Повторить упавшие» обязано брать и недоведённые операции."""
    pool = _QueuePool([_partial_op()])
    submitted: list[tuple] = []

    async def _fake_submit(p, uid, op_type, params, **kw):
        submitted.append((uid, op_type, kw.get("total_items")))
        return 1

    monkeypatch.setattr(mini_app_api._obus, "submit", _fake_submit)
    res = asyncio.run(mini_app_api._retry_failed_ops_core(pool, 555, 24))

    assert res["retried"] == 1, (
        "операция с 203 взятыми целями из 380 не предложена к повтору: "
        f"ответ {res}")
    assert submitted == [(555, "mass_invite", 380)]


def test_bulk_retry_still_skips_finished_and_cancelled(monkeypatch):
    """Обратная сторона: доведённое и отменённое повторять нельзя."""
    rows = [
        dict(_partial_op(1), status=op_status.DONE),
        dict(_partial_op(2), status=op_status.CANCELLED),
    ]
    pool = _QueuePool(rows)

    async def _fake_submit(*a, **kw):
        raise AssertionError("повторена операция, которую повторять не нужно")

    monkeypatch.setattr(mini_app_api._obus, "submit", _fake_submit)
    res = asyncio.run(mini_app_api._retry_failed_ops_core(pool, 555, 24))
    assert res["retried"] == 0


def test_escalation_notices_partial_operation():
    """Эскалация исчерпавших попытки видит недоведённую операцию."""
    pool = _QueuePool([_partial_op()])
    actions = asyncio.run(recovery_engine._operation_recovery(pool, None, 555))

    assert [a.target_id for a in actions] == [4242], (
        "операция, исчерпавшая попытки с частично сделанной работой, "
        f"не эскалирована: {actions}")
    assert actions[0].action == "escalate"


def test_escalation_ignores_successful_operation():
    pool = _QueuePool([dict(_partial_op(), status=op_status.DONE)])
    actions = asyncio.run(recovery_engine._operation_recovery(pool, None, 555))
    assert actions == []


def test_fleet_diagnosis_shows_reason_of_partial_operation():
    """Причина недоведённой операции попадает в «последние реальные ошибки»."""
    pool = _QueuePool([_partial_op()])
    diag = asyncio.run(fleet_doctor.diagnose(pool, 555))

    errors = diag.get("recent_errors") or []
    assert errors, (
        "у недоведённой операции есть причина, но диагностика флота показала "
        f"пустой список ошибок: {diag.get('recent_errors')!r}")
    assert "FloodWait" in errors[0]["error"]


def test_no_reader_of_unfinished_work_filters_failed_alone():
    """Храповик: три читателя недоделанной работы не возвращаются к 'failed'.

    Это целый класс регрессий: любой новый фильтр «только упавшие» снова
    спрячет от владельца операции с частично сделанной работой.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    places = {
        # Повтор на всех поверхностях (мини-апп и бот) идёт одной дверью шины.
        "services/operation_bus.py": "resubmit_unfinished",
        "services/operation_bus.py#one": "resubmit_one",
        "services/recovery_engine.py": "_operation_recovery",
        "services/fleet_doctor.py": "diagnose",
    }
    for key, func in places.items():
        rel = key.split("#")[0]
        src = (root / rel).read_text(encoding="utf-8")
        start = src.index(f"def {func}(")
        # конец функции — следующее определение на нулевом отступе
        m = re.search(r"\n(?:async def |def |class )", src[start:])
        body = src[start:start + (m.start() if m else len(src))]
        marker = ("UNFINISHED" if func == "resubmit_one"
                  else "sql_unfinished_list()")
        assert marker in body, (
            f"{rel}:{func} ищет недоделанную работу без {marker}")
        assert "status='failed'" not in body.replace(" ", ""), (
            f"{rel}:{func} снова фильтрует только status='failed' — "
            "операции с частично сделанной работой опять потеряются")
