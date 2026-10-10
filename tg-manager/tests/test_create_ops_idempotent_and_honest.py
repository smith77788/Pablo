"""Повтор создания не заводит второй комплект каналов, а частичный успех не
закрывается полным провалом.

СОЗДАНИЕ НЕОБРАТИМО. Повтор операции — событие штатное (сброс зависшей,
таймаут, сетевой сбой) — начинал пакет заново и создавал ВТОРОЙ комплект
каналов: лишние ресурсы в аккаунте, сожжённый дневной лимит создания и прямой
риск бана, потому что Telegram смотрит на темп создания.

Ключ идемпотентности здесь не название, а НОМЕР ШАГА. Название не годится: в
режиме «одно имя на все» оно одинаково у всего пакета (пропустили бы весь пакет
после первого элемента), а в SEO-режиме имена генерируются. Номер шага устойчив
по построению: цикл всегда идёт range(count) в том же порядке и с теми же
params.

ЧАСТИЧНЫЙ УСПЕХ. Итог операции считается по счётчикам (op_status.classify_final),
а не по слову исполнителя. Исполнители создания отдавали только `created`,
поэтому пакет, где часть каналов создана, а часть нет, приходил как
ok=0 / failed>0 и закрывался ПОЛНЫМ провалом — хотя каналы созданы и лежат в
аккаунте. Хуже того, такой «провал» шёл в предохранитель, и несколько частично
удавшихся пакетов подряд открывали цепь, останавливая ВСЕ операции владельца.
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from services import op_worker, op_status


_SRC = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


def _fn(name: str) -> str:
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"не найдена функция {name}")


# ── Частичный успех обязан быть partial, а не failed ────────────────────────

def test_partial_creation_is_not_a_total_failure():
    """7 созданных каналов из 10 — это partial, а не «ничего не вышло»."""
    assert op_status.classify_final("done", ok=7, failed=3) == op_status.PARTIAL


def test_creation_result_without_ok_would_read_as_failure():
    """Показываем цену пропущенного `ok`: без него тот же пакет — полный провал."""
    assert op_status.classify_final("done", ok=0, failed=3) == "failed"


@pytest.mark.parametrize("name", [
    "_exec_bulk_create_channels",
    "_exec_bulk_create_channels_multi",
    "_exec_global_presence_channel",
    "_exec_global_presence_bot",
])
def test_creation_executors_report_ok_counter(name):
    body = _fn(name)
    created = body.count('"created": created_count,')
    ok = body.count('"ok": created_count,')
    assert created and ok >= created, (
        f"{name}: каждый итог с `created` обязан нести и `ok` — иначе частично "
        f"удавшийся пакет закрывается полным провалом и бьёт в предохранитель"
    )


# ── Идемпотентность повтора по номеру шага ──────────────────────────────────

def test_helper_reads_only_successful_steps():
    body = _fn("completed_steps")
    assert "status='ok'" in body, "сделанными считаются только успешные шаги"
    # Скоуп сохраняется, но теперь он — ЦЕПОЧКА повторов: у операции,
    # поставленной кнопкой «Повторить», свой новый id, а журнал уже сделанной
    # работы лежит под id предка (op_worker.journal_op_ids). Неограниченное
    # чтение журнала по-прежнему запрещено.
    assert "op_id = ANY($1::bigint[])" in body, (
        "выборка обязана быть скоуплена операцией и её предками-повторами"
    )
    assert "journal_op_ids(pool, op_id)" in body, (
        "скоуп собран не по цепочке повторов"
    )
    assert "_safe_fetch(" in body, "сбой чтения журнала не должен ронять операцию"


@pytest.mark.parametrize("name,guard", [
    ("_exec_bulk_create_channels", "if num in _done_steps:"),
    ("_exec_bulk_create_channels_multi", "if (task_i + 1) in _done_steps:"),
])
def test_creation_skips_steps_done_by_previous_run(name, guard):
    body = _fn(name)
    assert "_done_steps = await completed_steps(pool, op_id)" in body, (
        f"{name}: повтор обязан знать, что уже создано"
    )
    assert guard in body, f"{name}: пропуск обязан сравниваться номером шага"
    # Пропуск — внутри цикла и ДО самого создания.
    loop = min(i for i in (body.find("for i in range(count):"),
                           body.find("for task_i in range(total_ops):")) if i >= 0)
    assert body.index("_done_steps = await") < loop
    assert loop < body.index(guard)
    assert body.index(guard) < body.index("account_manager.create_channel")


@pytest.mark.parametrize("name,guard", [
    ("_exec_bulk_create_channels", "if num in _done_steps:"),
    ("_exec_bulk_create_channels_multi", "if (task_i + 1) in _done_steps:"),
])
def test_skipped_step_counts_as_created(name, guard):
    """Иначе повтор отчитается «1 из 20» на уже сделанной работе."""
    body = _fn(name)
    skip = body[body.index(guard):]
    skip = skip[:skip.index("continue")]
    assert "created_count += 1" in skip, "созданный ранее канал — достигнутая цель"
    assert "failed_count" not in skip, "пропуск не провал"
    assert "done_items=done_items+1" in skip, "прогресс обязан двигаться и на пропуске"


def test_legacy_path_sets_total_items():
    """Без потолка прогресс рос от нуля к нулю: «7/0»."""
    body = _fn("_exec_bulk_create_channels")
    assert "UPDATE operation_queue SET total_items=$1 WHERE id=$2" in body
    m = re.search(r'total_items=\$1 WHERE id=\$2", (\w+), op_id', body)
    assert m and m.group(1) == "count", "потолок — размер пакета"


# ── Поведение помощника на живых данных ─────────────────────────────────────

class _FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, query, *args):
        return self.rows


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_completed_steps_reads_step_numbers():
    pool = _FakePool([{"step_num": 1}, {"step_num": 3}])
    assert _run(op_worker.completed_steps(pool, 42)) == {1, 3}


def test_completed_steps_is_empty_on_first_run():
    assert _run(op_worker.completed_steps(_FakePool([]), 42)) == set()


def test_completed_steps_never_raises_on_odd_rows():
    """Сбой чтения журнала не должен ронять операцию, ради которой он читается."""
    pool = _FakePool([{"nonsense": 1}, {"step_num": None}, {"step_num": "2"}])
    assert _run(op_worker.completed_steps(pool, 42)) == {2}
