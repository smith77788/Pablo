"""Поток событий Mini App: запросы параллельно, одинаковые снимки не шлются.

Цикл крутится вечно на КАЖДОЕ открытое приложение: раньше он каждые 15 секунд
делал десяток запросов к базе ПО ОЧЕРЕДИ и отправлял полные снимки, даже если
ничего не изменилось. На мобильной сети это постоянный трафик и постоянные
перерисовки на пустом месте.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from services import mini_app_api as m


class _SlowPool:
    """Пул, где каждый запрос стоит фиксированную задержку — видно, ждут ли по очереди."""

    def __init__(self, delay=0.05, counts=None):
        self.delay = delay
        self.calls = 0
        self.peak = 0
        self._live = 0
        self.counts = counts or {}

    async def _work(self, query):
        self.calls += 1
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            await asyncio.sleep(self.delay)
            for needle, value in self.counts.items():
                if needle in query:
                    return value
            return 0
        finally:
            self._live -= 1

    async def fetchval(self, query, *a):
        return await self._work(query)

    async def fetch(self, query, *a):
        await self._work(query)
        return []

    async def fetchrow(self, query, *a):
        await self._work(query)
        return None

    async def execute(self, query, *a):
        return await self._work(query)


def test_stats_queries_run_in_parallel():
    pool = _SlowPool(delay=0.05)
    started = time.monotonic()
    res = asyncio.run(m._stats(pool, 42))
    elapsed = time.monotonic() - started

    assert pool.calls == 7, "должно быть семь счётчиков"
    # По очереди — ~0.35 с; параллельно — около одной задержки.
    assert elapsed < 0.2, f"счётчики всё ещё считаются по очереди: {elapsed:.2f}s"
    assert pool.peak > 1
    assert set(res) == {"bots", "channels", "subscribers", "campaigns_active",
                        "funnels_active", "accounts", "ops_running"}


def test_stats_admin_scope_counts_the_whole_platform():
    seen = []

    class _P(_SlowPool):
        async def fetchval(self, query, *a):
            seen.append((" ".join(query.split()), a))
            return await self._work(query)

    asyncio.run(m._stats(_P(delay=0), 42, admin=True))
    acc = [q for q, a in seen if q.startswith("SELECT COUNT(*) FROM tg_accounts")]
    assert acc == ["SELECT COUNT(*) FROM tg_accounts"], \
        "у админа счётчик аккаунтов межтенантный, без owner_id"

    seen.clear()
    asyncio.run(m._stats(_P(delay=0), 42, admin=False))
    acc = [(q, a) for q, a in seen if q.startswith("SELECT COUNT(*) FROM tg_accounts")]
    assert acc and "owner_id=$1" in acc[0][0] and acc[0][1] == (42,)


def test_stats_survives_a_broken_counter():
    class _Broken(_SlowPool):
        async def fetchval(self, query, *a):
            if "funnel_subscriptions" in query:
                raise RuntimeError("нет таблицы")
            return await self._work(query)

    res = asyncio.run(m._stats(_Broken(delay=0), 42))
    assert res["funnels_active"] == 0, "сломанный счётчик не должен рушить дашборд"


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
