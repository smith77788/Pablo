"""Оплата тарифа снимает промо-брендинг с ботов клиента без перезапуска.

Кэш тарифа в `brand_injection` жил без срока и без сброса: после оплаты боты
клиента до следующего деплоя продолжали вставлять промо в рассылки и
авто-ответы (платящий клиент рассылал рекламу платформы своей аудитории), а
истёкшая подписка оставалась без брендинга. Проверяем оба выхода: сброс по
владельцу (его зовут оплата, триал и админка) и срок жизни записи.
"""
from __future__ import annotations

from unittest.mock import patch

from services import brand_injection as bi


class _Pool:
    async def fetchval(self, q, *a):
        return 42          # владелец бота


async def test_payment_invalidation_reaches_bot_brand_cache():
    plan = {"v": "free"}

    async def _get_plan(pool, uid):
        return plan["v"]

    bi.invalidate_cache()
    with patch("bot.utils.subscription.get_plan", _get_plan):
        assert await bi.is_free_tier(_Pool(), 7) is True
        plan["v"] = "paid"
        assert await bi.is_free_tier(_Pool(), 7) is True   # кэш

        from bot.utils.subscription import invalidate_plan_cache
        invalidate_plan_cache(42)                          # оплата владельца
        assert await bi.is_free_tier(_Pool(), 7) is False


async def test_cache_entry_expires(monkeypatch):
    plan = {"v": "paid"}

    async def _get_plan(pool, uid):
        return plan["v"]

    bi.invalidate_cache()
    t = {"now": 1000.0}
    monkeypatch.setattr(bi.time, "monotonic", lambda: t["now"])
    with patch("bot.utils.subscription.get_plan", _get_plan):
        assert await bi.is_free_tier(_Pool(), 8) is False
        assert await bi.is_user_free_tier(_Pool(), 9) is False
        plan["v"] = "free"                                 # подписка истекла
        t["now"] += bi._PLAN_CACHE_TTL + 1
        assert await bi.is_free_tier(_Pool(), 8) is True
        assert await bi.is_user_free_tier(_Pool(), 9) is True
    bi.invalidate_cache()
