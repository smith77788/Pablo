"""/start и главная мини-аппа не ходят в базу по одному запросу за раз.

Владелец жаловался: бот на /start и главная мини-аппа отвечают медленно. Замер
(база с задержкой 20 мс туда-обратно) показал, что почти всё время — ожидание
сети: независимые выборки шли строго друг за другом. «Итоги» — шесть
запросов подряд плюс восемь блоков «требует внимания» подряд (~0,9 с),
онбординг — десяток подряд, губернатор флота и давление — ещё по несколько.

Здесь проверено поведение, а не текст: поддельный пул отвечает с паузой и
считает, сколько запросов было в полёте одновременно. Последовательный код
даёт ровно 1 — и тест краснеет; разом — больше.
"""
from __future__ import annotations

import asyncio

DELAY = 0.02


class _Rec(dict):
    """Строка-ответ: любой ключ — 0, как у пустой выборки COUNT(*)."""

    def __missing__(self, key):
        return 0

    def keys(self):  # noqa: D401 — как у asyncpg.Record
        return []


class ConcurrencyPool:
    def __init__(self):
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    async def _hit(self, ret):
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(DELAY)
            return ret
        finally:
            self.inflight -= 1

    async def fetch(self, *a, **k):
        return await self._hit([])

    async def fetchrow(self, *a, **k):
        return await self._hit(_Rec())

    async def fetchval(self, *a, **k):
        return await self._hit(0)

    async def execute(self, *a, **k):
        return await self._hit("OK")


def test_results_summary_queries_go_together():
    from services import results_summary

    pool = ConcurrencyPool()
    out = asyncio.run(results_summary.compute(pool, 1, 7, with_attention=False))
    assert out["leads"]["value"] == 0
    # leads, replies, reach, orders, deals, has_resources — все разом.
    assert pool.peak >= 6, f"запросы «Итогов» идут по одному (пик {pool.peak})"


def test_attention_blocks_go_together(monkeypatch):
    from services import mini_app_dashboard, mini_app_api, operation_bus, op_worker

    async def _no_frozen(_pool):
        return None

    async def _no_active():
        return frozenset()

    async def _health(_pool, _uid):
        return {}

    monkeypatch.setattr(operation_bus, "queue_frozen", _no_frozen)
    monkeypatch.setattr(op_worker, "active_op_ids", _no_active)
    monkeypatch.setattr(mini_app_api, "account_health_summary", _health)
    pool = ConcurrencyPool()
    res = asyncio.run(mini_app_dashboard.attention_items(pool, 1))
    assert res["items"] == []
    assert pool.peak >= 5, f"блоки «требует внимания» идут по одному (пик {pool.peak})"


def test_attention_keeps_card_order(monkeypatch):
    """Разом — но порядок карточек прежний: блоки склеиваются в исходном
    порядке, а не в порядке, в каком ответила база."""
    from services import mini_app_dashboard, mini_app_api, operation_bus, op_worker

    async def _no_frozen(_pool):
        return None

    async def _no_active():
        return frozenset()

    async def _health(_pool, _uid):
        return {}

    monkeypatch.setattr(operation_bus, "queue_frozen", _no_frozen)
    monkeypatch.setattr(op_worker, "active_op_ids", _no_active)
    monkeypatch.setattr(mini_app_api, "account_health_summary", _health)

    class P(ConcurrencyPool):
        async def fetchval(self, sql, *a, **k):
            # Молчащие боты отвечают раньше, каналы — позже: порядок
            # одинаковой важности обязан остаться исходным.
            if "managed_channels" in sql:
                await asyncio.sleep(0.01)
                return 3
            if "account_rehab_state" in sql:
                await asyncio.sleep(0.03)
                return 3
            return await self._hit(0)

    res = asyncio.run(mini_app_dashboard.attention_items(P(), 1))
    kinds = [i["kind"] for i in res["items"]]
    assert kinds == ["acc_rehab", "ch_stale"], kinds


def test_onboarding_state_reads_go_together():
    from services import onboarding

    pool = ConcurrencyPool()
    asyncio.run(onboarding.load_state(pool, 1))
    assert pool.peak >= 4, f"онбординг читает по одному (пик {pool.peak})"


def test_plan_sources_read_together():
    from bot.utils import subscription

    subscription._plan_cache.pop(987654321, None)
    pool = ConcurrencyPool()
    assert asyncio.run(subscription.get_plan(pool, 987654321)) == "free"
    assert pool.peak == 2, f"источники тарифа читаются по одному (пик {pool.peak})"


def test_infra_pressure_reads_go_together():
    from services import infra_pressure

    pool = ConcurrencyPool()
    out = asyncio.run(infra_pressure.compute_pressure(pool, 1))
    assert "score" in out
    assert pool.peak >= 3, f"давление флота читается по одному (пик {pool.peak})"


def test_start_counts_and_summary_go_together(monkeypatch):
    """/start вернувшегося владельца: счётчик рассылок, «итоги недели» и
    отметка входа идут одной пачкой, а не отдельными походами в базу."""
    from unittest.mock import AsyncMock

    from aiogram.types import Message

    from bot.handlers import start as st
    from database import db

    async def _info(_pool, _uid):
        return {"user_id": 1}

    async def _bots(_pool, _uid):
        return [{"bot_id": 10, "audience_count": 5}]

    monkeypatch.setattr(db, "get_user_info", _info)
    monkeypatch.setattr(db, "get_bots", _bots)
    pool = ConcurrencyPool()
    msg = Message.model_validate({
        "message_id": 1, "date": 0, "text": "/start",
        "chat": {"id": 77, "type": "private"},
        "from": {"id": 77, "is_bot": False, "first_name": "T"}})
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)

    async def go():
        await st.cmd_start(msg, pool)
        await asyncio.sleep(0.1)   # дождаться фоновой отметки входа

    asyncio.run(go())
    assert answer.await_count == 1
    assert "Infragram OS" in answer.await_args.args[0]
    assert pool.peak >= 6, f"/start ходит в базу по одному запросу (пик {pool.peak})"
