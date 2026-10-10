"""Инварианты единого источника тарифов (bot/utils/tariffs.py).

Эти тесты фиксируют правила, которые легко нечаянно нарушить при правке
тарифов и создать «расхождения»: free всегда ≤ paid, неизвестный план
безопасно скатывается во free, неизвестная фича — в paid (fail-closed),
цена берётся из config (env), а не хардкодится.
"""

from __future__ import annotations

import importlib

from bot.utils import tariffs


def test_two_tiers_only():
    assert tariffs.PLANS == ("free", "paid")
    assert set(tariffs.PLAN_LEVELS) == {"free", "paid"}
    assert tariffs.PLAN_LEVELS["free"] < tariffs.PLAN_LEVELS["paid"]


def test_free_never_exceeds_paid_on_any_resource():
    for res in ("bots", "channels"):
        free = tariffs.resource_limit(res, "free")
        paid = tariffs.resource_limit(res, "paid")
        assert free <= paid, f"{res}: free {free} > paid {paid}"


def test_paid_resources_are_unlimited_by_default():
    for res in ("bots", "channels"):
        assert tariffs.is_unlimited(tariffs.resource_limit(res, "paid"))


def test_resource_limits_keyed_by_canonical_plans_only():
    # Корень бага миграции: словарь, ключёванный не 'free'/'paid', ломает
    # d.get('paid'). Любой ресурс из tariffs обязан иметь ровно эти ключи.
    for res in ("bots", "channels", "accounts", "ranking_keywords", "auto_reply_rules"):
        assert set(tariffs.resource_limits(res)) == {"free", "paid"}, res


def test_new_resource_defaults():
    assert tariffs.resource_limit("accounts", "free") == 0
    assert tariffs.is_unlimited(tariffs.resource_limit("accounts", "paid"))
    assert tariffs.resource_limit("ranking_keywords", "free") == 0
    assert tariffs.is_unlimited(tariffs.resource_limit("ranking_keywords", "paid"))
    assert tariffs.resource_limit("auto_reply_rules", "free") == 5
    assert tariffs.is_unlimited(tariffs.resource_limit("auto_reply_rules", "paid"))


def test_unknown_plan_fails_safe_to_free():
    assert tariffs.coerce_plan("totally-unknown") == "free"
    assert tariffs.coerce_plan(None) == "free"
    assert tariffs.coerce_plan("") == "free"


def test_legacy_aliases_map_to_paid():
    for alias in ("pro", "starter", "enterprise", "max", "maximum"):
        assert tariffs.normalize_plan(alias) == "paid"
        assert tariffs.coerce_plan(alias) == "paid"


def test_unknown_feature_is_fail_closed():
    # Новая, ещё не размеченная фича не должна случайно стать бесплатной.
    assert tariffs.feature_plan("brand_new_feature_xyz") == "paid"


def test_every_declared_feature_resolves_to_a_valid_plan():
    for key in tariffs.feature_keys():
        assert tariffs.feature_plan(key) in tariffs.PLAN_LEVELS, key


def test_basic_bots_is_free():
    assert tariffs.feature_plan("basic_bots") == "free"


def test_price_is_sourced_from_config_not_hardcoded():
    from config import PLAN_PRICES_USD

    assert tariffs.price_usd("free") == 0
    assert tariffs.price_str("free") == "Free"
    # Платная цена обязана совпадать с config (который читает env PRICE_PAID).
    assert tariffs.price_usd("paid") == int(PLAN_PRICES_USD["paid"])
    assert tariffs.price_str("paid") == f"${int(PLAN_PRICES_USD['paid'])}"


def test_unlimited_formatting():
    assert tariffs.format_limit(tariffs.UNLIMITED) == "∞"
    assert tariffs.format_limit(5) == "5"
    assert tariffs.is_unlimited(tariffs.UNLIMITED)
    assert not tariffs.is_unlimited(5)


def test_env_override_for_limit(monkeypatch):
    monkeypatch.setenv("LIMIT_FREE_BOTS", "2")
    assert tariffs.resource_limit("bots", "free") == 2
    monkeypatch.setenv("LIMIT_FREE_BOTS", "unlimited")
    assert tariffs.is_unlimited(tariffs.resource_limit("bots", "free"))
    monkeypatch.setenv("LIMIT_FREE_BOTS", "-1")
    assert tariffs.is_unlimited(tariffs.resource_limit("bots", "free"))


def test_env_override_for_feature_plan(monkeypatch):
    monkeypatch.setenv("FEATURE_PLAN_CRM", "free")
    assert tariffs.feature_plan("crm") == "free"


def test_subscription_module_projects_tariffs_consistently():
    # Публичные проекции в subscription.py не должны разойтись с источником.
    sub = importlib.import_module("bot.utils.subscription")
    assert sub.BOT_LIMITS == tariffs.resource_limits("bots")
    assert sub.CHANNEL_LIMITS == tariffs.resource_limits("channels")
    assert sub.PLAN_LEVELS == dict(tariffs.PLAN_LEVELS)
    assert sub.FEATURE_PLAN == tariffs.feature_plan_map()
    assert sub.coerce_plan("pro") == "paid"
    assert sub.feature_required_plan("crm") == tariffs.feature_plan("crm")
