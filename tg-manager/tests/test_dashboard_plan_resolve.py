"""Резолв тарифа для дашборда: «Enterprise, а мини-апп показывает Free».

Баг (жалоба пользователя со скриншота): подписка Enterprise, но мини-апп
отображает Free. Причина — логика дашборда была `plan = get_plan(); if not plan:
fallback на platform_users.current_plan`. Но get_plan возвращает "free" (истинную
строку), поэтому фолбэк НЕ срабатывал: тариф, выданный админом только в
platform_users (без активной строки в subscriptions), терялся → Free.

Фикс — `_resolve_best_plan`: наивысший тариф среди ВСЕХ источников. Каждый
источник уже отфильтрован по сроку в своём SQL-запросе; функция лишь выбирает
максимум и приводит к канону (free/paid).
"""
from __future__ import annotations

from services.mini_app_api import _resolve_best_plan


def test_platform_users_plan_wins_over_free_get_plan():
    """Ядро бага: get_plan='free', но в platform_users выдан enterprise → paid."""
    assert _resolve_best_plan("free", None, "enterprise") == "paid"


def test_active_subscription_makes_paid_despite_stale_cache():
    """Кеш get_plan устарел ('free'), но активная подписка доказывает платный тариф."""
    assert _resolve_best_plan("free", "pro", None) == "paid"


def test_all_free_stays_free():
    assert _resolve_best_plan("free", None, None) == "free"
    assert _resolve_best_plan(None, None, None) == "free"


def test_get_plan_paid_is_respected():
    assert _resolve_best_plan("paid", None, None) == "paid"


def test_marketing_aliases_coerced_to_paid():
    """Любой платный алиас (enterprise/pro/max/starter) → канонический paid."""
    for alias in ("enterprise", "pro", "max", "maximum", "starter"):
        assert _resolve_best_plan(None, None, alias) == "paid", alias


def test_unknown_or_empty_values_ignored():
    """Мусор/пустые значения не роняют резолвер и не поднимают тариф."""
    assert _resolve_best_plan("", "   " and None, None) == "free"
    # неизвестная строка коэрсится в free (не выше)
    assert _resolve_best_plan("garbage", None, None) == "free"
