"""«Позиции в поиске» читают прошлые позиции одним запросом, а не на строку.

Экраны `vis_all_positions` и `vis_by_bot` делали запрос в
`position_history` на каждое слово списка — до 30 подряд, пока владелец ждёт
ответа на кнопку. Здесь на живой базе: стрелки тренда прежние, а запрос
истории — один. И попутно: прошлая позиция берётся по id бота из самой
строки рейтинга, а не по username через `managed_bots` без владельца.

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_521
BOT = 990_521_001


class _Counting:
    def __init__(self, pool):
        self._p = pool
        self.history_reads = 0

    def _see(self, q):
        if "position_history" in q:
            self.history_reads += 1

    async def fetch(self, q, *a, **k):
        self._see(q)
        return await self._p.fetch(q, *a, **k)

    async def fetchrow(self, q, *a, **k):
        self._see(q)
        return await self._p.fetchrow(q, *a, **k)

    async def fetchval(self, q, *a, **k):
        self._see(q)
        return await self._p.fetchval(q, *a, **k)


def _go(body):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await _clean(p)
            await _seed(p)
            await body(p)
        finally:
            await _clean(p)
            await p.close()

    asyncio.run(_main())


async def _clean(p):
    await p.execute("DELETE FROM position_history WHERE bot_id=$1", BOT)
    await p.execute("DELETE FROM tracked_keywords WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM managed_bots WHERE added_by=$1", OWNER)


# слово → (история позиций от старой к новой; последняя — текущая)
_HISTORY = {
    "кофе": [9, 4],          # поднялся с 9 на 4
    "чай": [3, 7],           # упал с 3 на 7
    "какао": [5],            # истории нет — без дельты
}


async def _seed(p):
    await p.execute(
        "INSERT INTO managed_bots (token, bot_id, username, added_by) "
        "VALUES ('x', $1, 'rank_test_bot', $2)", BOT, OWNER)
    for i, (kw, hist) in enumerate(_HISTORY.items()):
        kid = await p.fetchval(
            "INSERT INTO tracked_keywords (bot_id, owner_id, keyword) "
            "VALUES ($1, $2, $3) RETURNING id", BOT, OWNER, kw)
        for j, pos in enumerate(hist):
            await p.execute(
                "INSERT INTO position_history (bot_id, keyword, position, checked_at) "
                "VALUES ($1, $2, $3, now() - make_interval(hours => $4))",
                BOT, kw, pos, len(hist) - j)
        await p.execute(
            "INSERT INTO search_rankings (keyword_id, bot_id, position, checked_at) "
            "VALUES ($1, $2, $3, now() - make_interval(mins => $4))",
            kid, BOT, hist[-1], i)


def _callback():
    return SimpleNamespace(from_user=SimpleNamespace(id=OWNER), answer=None)


def _run_screen(monkeypatch, pool, handler, data):
    from bot.handlers import ranking

    shown = []

    async def _edit(cb, text, **kw):
        shown.append(text)

    async def _answer(*a, **k):
        return None

    monkeypatch.setattr(ranking, "safe_edit", _edit)
    monkeypatch.setattr(ranking, "safe_answer", _answer)
    cp = _Counting(pool)
    return cp, shown, handler(_callback(), data, cp)


def _assert_trends(text):
    assert "«кофе» → #4 (↑ с #9)" in text
    assert "«чай» → #7 (↓ с #3)" in text
    assert "«какао» → #5" in text and "«какао» → #5 (" not in text


def test_all_positions_reads_history_once(monkeypatch):
    from bot.handlers import ranking

    async def body(pool):
        cp, shown, coro = _run_screen(
            monkeypatch, pool, ranking.vis_all_positions,
            ranking.VisCb(action="all_positions"))
        await coro
        assert cp.history_reads == 1
        _assert_trends(shown[-1])

    _go(body)


def test_by_bot_reads_history_once(monkeypatch):
    from bot.handlers import ranking

    async def body(pool):
        cp, shown, coro = _run_screen(
            monkeypatch, pool, ranking.vis_by_bot,
            ranking.VisCb(action="by_bot", bot_id=BOT))
        await coro
        assert cp.history_reads == 1
        _assert_trends(shown[-1])

    _go(body)
