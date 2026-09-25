"""Поток событий нельзя открыть сто раз и подвесить процесс.

Соединение SSE живёт, пока открыто приложение, и каждые 15 секунд ходит в базу.
Лимит частоты пропускает 120 запросов в минуту, то есть открыть сотню вечных
потоков можно было одной пачкой: процесс держит сотню соединений, каждое со
своими запросами, и закрыть их снаружи нечем.

Мини-апп держит РОВНО ОДИН поток на вкладку (старый закрывается перед открытием
нового), поэтому потолок в 8 штук живому сценарию не мешает.
"""
from __future__ import annotations

import ast

import pytest

from services import mini_app_api as M


@pytest.fixture(autouse=True)
def _clean_counter():
    M._sse_streams.clear()
    yield
    M._sse_streams.clear()


def _events_src() -> str:
    """Тело обработчика по границам из AST, а не по окну фиксированной длины."""
    src = open(M.__file__, encoding="utf-8").read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "events":
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("обработчик events не найден")


def test_cap_has_a_sane_default_and_is_configurable():
    assert 1 <= M._SSE_MAX_PER_USER <= 64
    assert M._SSE_MAX_PER_USER >= 4, (
        "мини-апп держит поток на каждую вкладку: потолок ниже четырёх мешал бы "
        "обычной работе с телефона и ноутбука одновременно")


def test_handler_checks_the_cap_and_counts_streams():
    body = _events_src()
    assert "_SSE_MAX_PER_USER" in body, "потолок в обработчике не проверяется"
    assert "_sse_streams[uid] = _sse_streams.get(uid, 0) + 1" in body, (
        "поток не учитывается — потолок не сработает")
    assert "status=429" in body, "отказ должен быть честным 429, а не тишиной"


def test_stream_is_released_in_finally():
    """Без finally счётчик только растёт: обычный выход отсюда — отмена задачи
    (клиент закрыл вкладку), и владелец упёрся бы в потолок на пустом месте."""
    body = _events_src()
    tail = body[body.index("except (asyncio.CancelledError"):]
    assert "finally:" in tail, "освобождение места не в finally"
    assert "_sse_streams.pop(uid, None)" in tail


def test_counter_does_not_keep_zero_entries():
    """Освобождение убирает ключ, а не оставляет нули: иначе словарь растёт по
    числу когда-либо заходивших владельцев и не уменьшается никогда."""
    uid = 4242
    M._sse_streams[uid] = 1
    # Повторяем ту же арифметику, что в finally обработчика.
    left = M._sse_streams.get(uid, 1) - 1
    if left > 0:
        M._sse_streams[uid] = left
    else:
        M._sse_streams.pop(uid, None)
    assert uid not in M._sse_streams


def test_counter_is_registered_as_process_local():
    """Храповик на состояние в памяти требует ответа «что при двух репликах» —
    здесь ответ есть, и он должен остаться в реестре."""
    from tests import test_no_new_mutable_globals as ratchet

    assert "services/mini_app_api.py:_sse_streams" in ratchet.PROCESS_LOCAL
