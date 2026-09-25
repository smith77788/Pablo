"""Поток событий Mini App: мало запросов, одинаковые снимки не шлются.

Цикл крутится вечно на КАЖДОЕ открытое приложение: раньше он каждые 15 секунд
делал десяток запросов к базе и отправлял полные снимки, даже если ничего не
изменилось. На мобильной сети это постоянный трафик и постоянные перерисовки на
пустом месте.

Запросов стало четыре вместо десятка (семь счётчиков дашборда уходят одним), и
идут они по очереди, а не пачкой: такту раз в 15 секунд спешить некуда, а пачка
берёт вчетверо больше одновременных соединений из пула на каждое приложение.
"""
from __future__ import annotations

import ast
import asyncio
import json
import time

import pytest

from services import mini_app_api as m


class _FakePool:
    """Пул-заглушка: записывает запросы и умеет ронять объединённый запрос."""

    def __init__(self, combined_ok=True, value=0):
        self.queries: list[str] = []
        self.combined_ok = combined_ok
        self.value = value

    async def fetchrow(self, query, *a):
        self.queries.append(query)
        if not self.combined_ok:
            raise RuntimeError("нет такой таблицы")
        from services.mini_app_api import _STATS_KEYS
        return {k: self.value for k in _STATS_KEYS}

    async def fetchval(self, query, *a):
        self.queries.append(query)
        return self.value


def test_stats_uses_a_single_combined_query():
    pool = _FakePool(value=3)
    res = asyncio.run(m._stats(pool, 42))
    assert len(pool.queries) == 1, "счётчики должны уходить одним запросом"
    assert res == {k: 3 for k in m._STATS_KEYS}


def test_stats_falls_back_to_separate_counts():
    """Сломанный объединённый запрос не должен обнулять весь дашборд."""
    pool = _FakePool(combined_ok=False, value=5)
    res = asyncio.run(m._stats(pool, 42))
    assert len(pool.queries) == 1 + len(m._STATS_KEYS)
    assert res == {k: 5 for k in m._STATS_KEYS}


def test_admin_scope_drops_owner_filter_for_accounts():
    pool = _FakePool()
    asyncio.run(m._stats(pool, 42, admin=True))
    combined = " ".join(pool.queries[0].split())
    assert "(SELECT COUNT(*) FROM tg_accounts) AS accounts" in combined, \
        "у админа счётчик аккаунтов межтенантный"

    pool2 = _FakePool()
    asyncio.run(m._stats(pool2, 42, admin=False))
    combined2 = " ".join(pool2.queries[0].split())
    assert "(SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1) AS accounts" in combined2


def test_admin_fallback_does_not_pass_an_unused_parameter():
    """Межтенантный счётчик $1 не использует: лишний параметр обнулил бы его."""
    seen = []

    class _P(_FakePool):
        async def fetchrow(self, query, *a):
            raise RuntimeError("нет")

        async def fetchval(self, query, *a):
            seen.append((" ".join(query.split()), a))
            return 1

    asyncio.run(m._stats(_P(), 42, admin=True))
    acc = [(q, a) for q, a in seen if q == "SELECT COUNT(*) FROM tg_accounts"]
    assert acc and acc[0][1] == (), "запросу без $1 параметр слать нельзя"


# ── Отправка только изменившихся снимков ────────────────────────────────────

class _FakeResponse:
    def __init__(self):
        self.chunks: list[bytes] = []

    async def write(self, data: bytes):
        self.chunks.append(data)

    def events(self) -> list[str]:
        out = []
        for c in self.chunks:
            text = c.decode()
            if text.startswith("event: "):
                out.append(text.split("\n", 1)[0][len("event: "):])
        return out


def _make_push(resp):
    """Повторяет push из events(): та же семантика only_if_changed."""
    last: dict[str, str] = {}

    async def push(event, data, *, only_if_changed=False):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        if only_if_changed and last.get(event) == payload:
            return
        last[event] = payload
        await resp.write(f"event: {event}\ndata: {payload}\n\n".encode())

    return push


def test_unchanged_snapshot_is_not_resent():
    resp = _FakeResponse()
    push = _make_push(resp)

    async def _go():
        for _ in range(4):
            await push("stats", {"bots": 3}, only_if_changed=True)
        await push("stats", {"bots": 4}, only_if_changed=True)

    asyncio.run(_go())
    assert resp.events() == ["stats", "stats"], "повтор одинакового снимка не шлём"


def _events_src() -> str:
    """Тело обработчика events по ГРАНИЦАМ из AST, а не по окну фиксированной длины.

    Окно промахивается, как только код сдвинулся, и отрицательная проверка
    внутри него молча становится правдой — защита выключается сама собой.
    Обработчик вложен в setup_routes, поэтому обходим всё дерево.
    """
    src = open(m.__file__, encoding="utf-8").read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "events":
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("обработчик events не найден")


def test_source_keeps_the_tick_sequential_and_filters_repeats():
    """Такт не должен брать соединения пачкой, и повторы не должны уходить.

    gather в этом цикле ускоряет такт, которому спешить некуда (раз в 15
    секунд), но берёт вчетверо больше одновременных соединений. А такты разных
    приложений выстраиваются в ряд после каждого передеплоя: все
    переподключаются одновременно и дальше тикают синхронно.
    """
    body = _events_src()
    assert "asyncio.gather(" not in body, (
        "выборки такта снова берут соединения пачкой — на пуле в 20 это "
        "выбивает остальные запросы владельца в очередь")
    assert "only_if_changed=True" in body
    assert "_seen_order.popleft()" in body, (
        "завершённые операции должны вытесняться по одной, иначе после полной "
        "очистки набора они уедут клиенту повторно")
