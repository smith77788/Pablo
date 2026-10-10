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
    # План резолвится по НАИВЫСШЕМУ тарифу среди источников: get_plan, активная
    # подписка (exp_row.plan) и ручная выдача (platform_users.current_plan). Так
    # платный тариф не теряется ни при устаревшем кеше get_plan, ни когда подписка
    # выдана только в platform_users («Enterprise → Free»). Поведение — в
    # test_dashboard_plan_resolve.py; здесь проверяем ПРОВОДКУ всех трёх источников.
    m = re.search(
        r'stats\["plan"\]\s*=\s*_resolve_best_plan\(\s*\n\s*'
        r'r\["plan"\],.*?'
        r'r\["exp_row"\]\["plan"\].*?'
        r'plan_row\["current_plan"\]',
        _SRC, re.DOTALL,
    )
    assert m, (
        "dashboard должен резолвить план через _resolve_best_plan по всем трём "
        "источникам (get_plan + активная подписка + platform_users), иначе теряется "
        "платный тариф при устаревшем кеше или ручной выдаче в platform_users"
    )


def test_subscription_endpoint_uses_direct_subscriptions_authority():
    # /subscription резолвит тариф по НАИВЫСШЕМУ среди источников (тот же принцип,
    # что у дашборда), а не по мёртвому фолбэку `if not plan:` — иначе тариф из
    # subscriptions/platform_users терялся, когда get_plan вернул «free».
    m = re.search(r"async def subscription\(.*?\n(.*?)\n    async def ", _SRC, re.DOTALL)
    assert m, "subscription handler not found"
    body = m.group(1)
    assert "FROM subscriptions" in body, (
        "/subscription должен использовать прямой запрос subscriptions как авторитет"
    )
    assert "_resolve_best_plan(" in body and "platform_users" in body, (
        "/subscription должен брать наивысший тариф среди get_plan/subscriptions/"
        "platform_users через _resolve_best_plan (как дашборд)"
    )
