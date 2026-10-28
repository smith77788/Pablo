"""Отметка в журнале целей пишется только через дверь с повтором.

РАЗРЫВ. `operation_log` — источник правды о том, что уже сделано: по нему
двери идемпотентности (`completed_targets`, `completed_steps`,
`settled_targets`, `completed_account_targets`) решают, какие цели
возобновлённый прогон ПРОПУСТИТ. Для этого есть `_journal_write`: один
повтор на месте (блип лечится им), затем громкий `log.error` и счётчик.

Четыре исполнителя писали свои отметки в обход двери, обычным
`pool.execute`. Исход у двух вариантов разный, но одинаково плохой:

* без try/except сбой записи ронял весь прогон ПОСЛЕ сделанного действия —
  пост опубликован, чат вступлен, канал создан, а отметки нет: повтор идёт
  по этой цели второй раз гарантированно;
* с `try/except → log` отметка исчезала молча, и повтор делал то же самое.

ЧТО ПРОВЕРЯЕМ (храповик). В исполнителе, который ЧИТАЕТ дверь, запись в
`operation_log` обязана идти через `_journal_write`/`_journal_done`. Детектор
проверен на заведомо больном и заведомо здоровом примере — иначе чинить по
его списку нельзя.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

DOORS = {"completed_targets", "completed_steps", "settled_targets",
         "completed_account_targets"}
RAW = {"execute", "_safe_execute", "executemany"}


def _raw_journal_writes(src: str) -> dict[str, list[int]]:
    """{имя исполнителя: строки} — записи в журнал в обход двери."""
    tree = ast.parse(src)
    out: dict[str, list[int]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        calls = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if not (calls & DOORS):
            continue  # дверь не читается — отметка здесь только отчёт
        bad = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in RAW:
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "INSERT INTO operation_log" in seg:
                bad.append(node.lineno)
        if bad:
            out[fn.name] = sorted(bad)
    return out


def test_no_handler_writes_the_journal_behind_the_doors_back():
    src = open(SRC, encoding="utf-8").read()
    bad = _raw_journal_writes(src)
    assert not bad, (
        "эти исполнители читают дверь идемпотентности, но пишут отметку в "
        "журнал мимо `_journal_write` — сбой записи заставит повтор сделать "
        "цель второй раз:\n  " + "\n  ".join(
            f"{k}: строки {v}" for k, v in sorted(bad.items())))


# ── Самопроверка детектора: без неё чинить по его списку нельзя ──────────────

_SICK = '''
async def _exec_something(pool, op_id):
    done = await completed_targets(pool, op_id)
    await pool.execute(
        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
        op_id, 1, "t")
'''

_HEALTHY_DOOR = '''
async def _exec_something(pool, op_id):
    done = await completed_targets(pool, op_id)
    await _journal_write(
        pool,
        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
        op_id, 1, "t")
'''

# Отметка в исполнителе БЕЗ двери — это отчёт, а не предохранитель: повтор
# всё равно идёт по всему списку, и терять тут нечего кроме строки в деталях.
_HEALTHY_NO_DOOR = '''
async def _exec_something(pool, op_id):
    await pool.execute(
        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
        op_id, 1, "t")
'''


def test_the_detector_bites_the_sick_example():
    assert list(_raw_journal_writes(_SICK)) == ["_exec_something"]


def test_the_detector_leaves_the_healthy_examples_alone():
    assert _raw_journal_writes(_HEALTHY_DOOR) == {}
    assert _raw_journal_writes(_HEALTHY_NO_DOOR) == {}
