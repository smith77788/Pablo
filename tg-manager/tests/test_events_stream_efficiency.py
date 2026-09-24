"""Поток событий Mini App: запросы параллельно, одинаковые снимки не шлются.

Цикл крутится вечно на КАЖДОЕ открытое приложение: раньше он каждые 15 секунд
делал десяток запросов к базе ПО ОЧЕРЕДИ и отправлял полные снимки, даже если
ничего не изменилось. На мобильной сети это постоянный трафик и постоянные
перерисовки на пустом месте. Счётчики дашборда теперь уходят одним запросом —
не семью подряд и не семью соединениями из пула сразу.
"""
from __future__ import annotations

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


def test_source_wires_parallel_fetch_and_change_filter():
    """Сам цикл events() должен использовать оба приёма."""
    src = open(m.__file__, encoding="utf-8").read()
    start = src.index("async def events(request: web.Request)")
    body = src[start:start + 8000]
    assert "asyncio.gather(" in body, "выборки цикла должны идти одним заходом"
    assert "only_if_changed=True" in body
    assert "_seen_order.popleft()" in body, (
        "завершённые операции должны вытесняться по одной, иначе после полной "
        "очистки набора они уедут клиенту повторно")
