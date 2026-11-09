"""Ассистент выбирает аккаунт через общий подбор флота, а не своим запросом.

ЧТО БЫЛО. Четыре действия ассистента (создать канал, группу, бота, пачку
каналов) брали «активный аккаунт с наивысшим trust_score» собственным SELECT.
Аккаунт на паузе Telegram, под спам-блоком или в карантине выигрывал выбор —
и действие шло им, прямо под действующее ограничение.
"""
from __future__ import annotations

import asyncio
import inspect

from bot.utils import ai_tools


def test_the_assistant_asks_the_fleet_selector(monkeypatch):
    from services import flood_engine
    asked: list[tuple] = []

    async def _best(pool, owner_id, action_type="default", **kw):
        asked.append((owner_id, action_type))
        return None  # весь флот отдыхает

    monkeypatch.setattr(flood_engine, "get_best_account", _best)

    class _Pool:
        async def fetchrow(self, *a, **k):
            raise AssertionError("аккаунт выбран своим запросом мимо общего подбора")

    res = asyncio.run(ai_tools.action_create_channel(_Pool(), 5, "Новости"))
    assert asked == [(5, "create_channel")]
    assert "Нет свободных аккаунтов" in res["error"]
    res = asyncio.run(ai_tools.action_create_bot(_Pool(), 5, "Помощник"))
    assert asked[-1] == (5, "create_bot") and "error" in res


def test_no_private_top_trust_query_is_left():
    src = inspect.getsource(ai_tools)
    assert "ORDER BY trust_score DESC NULLS LAST LIMIT 1" not in src
