"""Строка успеха в журнале целей не теряется молча.

Журнал целей (`operation_log`) — не отчёт, а основание идемпотентности:
полтора десятка исполнителей читают его ПЕРЕД работой (`completed_targets`,
`completed_steps`, `settled_targets`) и пропускают цели, которые там уже
закрыты. Повтор операции видит и журнал предка (`journal_op_ids`), поэтому
продолжение не переделывает сделанное.

ЧТО БЫЛО. Строку успеха писали через `_safe_execute` — он ловит исключение,
пишет строчку в лог и возвращает 'ERROR'. То есть потерянная строка выглядела
как записанная. А потеря строки успеха — это не «неполный отчёт», а повторное
РЕАЛЬНОЕ действие на следующей попытке:

  * второй пост в канал, который его уже получил;
  * второе одинаковое сообщение живому человеку;
  * второй комплект созданных каналов и выданных прав.

Семнадцать мест теряли такую строку молча, и узнать об этом было негде: ни в
метриках, ни в ошибке операции.

ФИКС: одна дверь `_journal_done` — одна повторная попытка на месте, затем
log.error и счётчик. Операция не падает (цель уже обработана, падать поздно),
но потеря видна снаружи.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from services import op_worker

_SRC = (pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py")


class _Pool:
    def __init__(self, fail_first=0):
        self.fail_left = fail_first
        self.calls: list[tuple] = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        if self.fail_left > 0:
            self.fail_left -= 1
            raise RuntimeError("пул занят")
        return "INSERT 0 1"


def test_a_success_row_is_written_with_its_message():
    pool = _Pool()
    assert asyncio.run(op_worker._journal_done(pool, 7, 3, "ch#5", "опубликовано")) is True
    query, args = pool.calls[0]
    assert "INSERT INTO operation_log" in query and "'ok'" in query
    assert args == (7, 3, "ch#5", "опубликовано")


def test_a_transient_failure_is_retried_on_the_spot():
    pool = _Pool(fail_first=1)
    assert asyncio.run(op_worker._journal_done(pool, 7, 3, "ch#5")) is True
    assert len(pool.calls) == 2, "повторной попытки нет"


def test_a_lost_row_is_reported_not_swallowed():
    from services import metrics

    metrics.reset()
    pool = _Pool(fail_first=9)
    assert asyncio.run(op_worker._journal_done(pool, 7, 3, "ch#5")) is False, (
        "потерянная строка успеха возвращает «всё хорошо»: повтор сделает эту "
        "цель второй раз, и никто об этом не узнает")
    counters = metrics.snapshot().get("counters") or {}
    assert any("infragram_journal_write_failures_total" in str(k)
               for k in counters), f"потеря не попала в метрики: {counters}"
    assert ("infragram_journal_write_failures_total"
            in metrics._HELP), "у метрики нет описания — она не попадёт в выдачу"


def test_the_operation_survives_a_lost_row():
    """Цель уже обработана — падать поздно, исключение наружу не выходит."""
    pool = _Pool(fail_first=9)
    assert asyncio.run(op_worker._journal_done(pool, 7, 3, None)) is False


# ── Храповик на класс: успех не пишется глотающим путём ─────────────────────

# Служебные шаги — не цели (их отсекает operation_bus.REAL_TARGET_SQL), на
# идемпотентность они не влияют.
_SERVICE = ("'promote'", "'promote_trick'", "'link_fallback'", "'overflow'")


def _swallowing_success_writes(src: str) -> list[str]:
    """Вызовы `_safe_execute`, пишущие в журнал строку успеха.

    Границы берём по сбалансированным скобкам вызова (AST-разбор), а не окном
    фиксированной длины: окно рассыпалось бы от любой правки выше и выключило
    бы проверку молча.
    """
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "_safe_execute":
            continue
        sql = " ".join(
            a.value for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str))
        if "INSERT INTO operation_log" not in sql:
            continue
        if "'ok'" not in sql and "'skip'" not in sql:
            continue
        if any(s in sql for s in _SERVICE):
            continue
        out.append(" ".join(sql.split())[:110])
    return out


def test_the_detector_bites_on_a_known_sample():
    """Самопроверка измерителя до того, как верить его пустому списку."""
    bad = (
        "async def f():\n"
        "    await _safe_execute(pool, \"INSERT INTO operation_log(op_id, step_num, "
        "target, status) VALUES($1,$2,$3,'ok')\", op_id, i, t)\n")
    good_door = (
        "async def f():\n"
        "    await _journal_done(pool, op_id, i, t)\n")
    good_service = (
        "async def f():\n"
        "    await _safe_execute(pool, \"INSERT INTO operation_log(op_id, step_num, "
        "target, status) VALUES($1,0,'promote','ok')\", op_id)\n")
    good_error = (
        "async def f():\n"
        "    await _safe_execute(pool, \"INSERT INTO operation_log(op_id, step_num, "
        "target, status) VALUES($1,$2,$3,'error')\", op_id, i, t)\n")
    assert _swallowing_success_writes(bad)
    assert not _swallowing_success_writes(good_door)
    assert not _swallowing_success_writes(good_service)
    assert not _swallowing_success_writes(good_error)


def test_no_success_row_goes_through_the_swallowing_path():
    offenders = _swallowing_success_writes(_SRC.read_text(encoding="utf-8"))
    assert not offenders, (
        "эти места пишут строку успеха через _safe_execute: потеря выглядит как "
        "запись, и следующая попытка сделает цель второй раз — "
        f"{offenders}")
