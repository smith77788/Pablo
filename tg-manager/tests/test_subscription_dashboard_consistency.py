"""Регресс: дашборд мини-аппа показывает подписку так же, как бот и /subscription.

Баг (репорт пользователя «видят подписку в боте, но не в мини-аппе»): dashboard-
эндпоинт брал план из `_plan()` = get_plan (per-process кеш, TTL 60с) + фолбэк на
platform_users. Прямой запрос активной подписки (`exp_row` из subscriptions,
мимо кеша) использовался ТОЛЬКО для даты истечения, а НЕ для плана → при
устаревшем кеше get_plan (оплату обработал другой процесс — бот) дашборд
показывал «free» при живой подписке. Источник истины у /subscription и у бота —
таблица subscriptions; дашборд теперь тоже правит план по exp_row.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api

_SRC = inspect.getsource(mini_app_api)


def test_exp_row_query_selects_plan():
    # прямой запрос активной подписки должен тащить plan (не только expires_at),
    # чтобы дашборд мог выставить корректный тариф из источника истины.
    assert re.search(
        r'"exp_row":\s*pool\.fetchrow\(\s*\n?\s*"SELECT plan, expires_at FROM subscriptions',
        _SRC,
    ), "exp_row должен селектить plan из subscriptions"


def test_dashboard_corrects_plan_from_active_subscription():
    # при наличии активной подписки (exp_row) и «free»/пустом плане — правим на платный.
    m = re.search(
        r'if r\["exp_row"\] and \(not stats\.get\("plan"\) or stats\["plan"\] == "free"\):\s*\n\s*stats\["plan"\]',
        _SRC,
    )
    assert m, (
        "dashboard должен выставлять платный план, если есть активная подписка в "
        "subscriptions (иначе устаревший кеш get_plan → «free» при живой подписке)"
    )


def test_subscription_endpoint_uses_direct_subscriptions_authority():
    # /subscription правит план из прямого запроса subscriptions даже когда
    # get_plan вернул «free» — тот же принцип, что теперь у дашборда.
    m = re.search(r"async def subscription\(.*?\n(.*?)\n    async def ", _SRC, re.DOTALL)
    assert m, "subscription handler not found"
    body = m.group(1)
    assert "FROM subscriptions" in body and 'plan == "free"' in body, (
        "/subscription должен использовать прямой запрос subscriptions как авторитет"
    )
