"""Гейтинг тарифа: get_plan учитывает РУЧНУЮ выдачу (platform_users.current_plan).

Баг: подписку выдали вручную (platform_users.current_plan=enterprise), но
get_plan читал ТОЛЬКО subscriptions → возвращал free. Все гейты
(require_plan/require_feature/лимиты/operation_bus min_plan) резали такого
пользователя до free — «подписка есть, а функционал недоступен».

Фикс: get_plan резолвит НАИВЫСШИЙ тариф среди subscriptions И platform_users.
"""
from __future__ import annotations

import asyncio

import bot.utils.subscription as sub


class _FakePool:
    """Роутит fetchrow по содержимому SQL: subscriptions vs platform_users."""
    def __init__(self, sub_plan=None, pu_plan=None):
        self._sub_plan = sub_plan
        self._pu_plan = pu_plan

    async def fetchrow(self, q, *a):
        if "FROM subscriptions" in q:
            return {"plan": self._sub_plan} if self._sub_plan else None
        if "FROM platform_users" in q:
            return {"current_plan": self._pu_plan} if self._pu_plan else None
        return None


def _fresh_get_plan(pool, uid):
    sub._plan_cache.pop(uid, None)   # обойти per-process кеш
    return asyncio.run(sub.get_plan(pool, uid))


def test_manual_grant_in_platform_users_resolves_paid():
    # только ручная выдача, subscriptions пуст → раньше было free
    assert _fresh_get_plan(_FakePool(sub_plan=None, pu_plan="enterprise"), 424242) == "paid"


def test_subscription_only_still_paid():
    assert _fresh_get_plan(_FakePool(sub_plan="pro", pu_plan=None), 424243) == "paid"


def test_both_empty_is_free():
    assert _fresh_get_plan(_FakePool(sub_plan=None, pu_plan=None), 424244) == "free"


def test_platform_users_query_present_in_get_plan():
    import inspect
    src = inspect.getsource(sub.get_plan)
    assert "platform_users" in src and "current_plan" in src
