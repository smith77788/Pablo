"""Срабатывания дедупа постановки видны снаружи, а не только в логе.

ЗАЧЕМ ЭТОТ СЧЁТЧИК. Дедуп, который не срабатывает НИ РАЗУ, снаружи выглядит
ровно как работающий: код на месте, окно настроено, тест на двойной тап
зелёный. Именно так и было в двух местах, где в дедуп-ключ попадало значение,
своё у каждого тапа (указатель на сообщение с прогресс-баром; время запуска,
посчитанное от «сейчас»). Ноль срабатываний по типу операции, который владелец
запускает кнопкой, — это не «тапов не было», это «дедуп по нему слеп», и на
графике такой ноль виден сразу.

Счётчик вспомогателен: его сбой не повод терять операцию.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993619

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


COUNTER = "infragram_submit_deduped_total"


def _deduped(op_type: str) -> float:
    from services import metrics

    total = 0.0
    for key, value in metrics._counters.items():
        if key[0] == COUNTER and dict(key[1] or ()).get("op_type") == op_type:
            total += float(value)
    return total


def _submit(pool, text: str) -> int:
    from services import operation_bus

    return _run(operation_bus.submit(
        pool, OWNER, "run_broadcast", {"bot_id": 71, "text": text},
        total_items=3, bypass_plan_check=True))


def test_a_merged_second_tap_is_counted(pool):
    before = _deduped("run_broadcast")
    first = _submit(pool, "Один и тот же текст")
    second = _submit(pool, "Один и тот же текст")
    assert second == first, "дедуп не сработал — проверять нечего, перечитать"
    assert _deduped("run_broadcast") == before + 1, (
        "слияние повторного сабмита не посчитано: на графике дедуп, который "
        "работает, неотличим от дедупа, который слеп"
    )


def test_a_genuinely_new_operation_is_not_counted(pool):
    before = _deduped("run_broadcast")
    assert _submit(pool, "Первый текст") != _submit(pool, "Второй текст")
    assert _deduped("run_broadcast") == before, (
        "счётчик вырос на обычной постановке — такой график врёт в другую "
        "сторону и прячет настоящую слепоту"
    )


def test_the_counter_is_described_for_the_owner():
    from services.metrics import _HELP

    assert COUNTER in _HELP, "счётчик без описания — строка без подписи на графике"
    assert re.search(r"[а-яА-Я]", _HELP[COUNTER]), "описание читает владелец"


def test_the_counter_cannot_break_the_submission():
    import ast
    import pathlib

    from services import operation_bus

    src = pathlib.Path(operation_bus.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "submit")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        body = ast.get_source_segment(src, node) or ""
        if COUNTER in body and any(
                isinstance(h.type, ast.Name) and h.type.id == "Exception"
                for h in node.handlers):
            guarded = True
    assert guarded, (
        f"{COUNTER} пишется вне try/except Exception — сбой вспомогательного "
        "счётчика потеряет операцию"
    )
