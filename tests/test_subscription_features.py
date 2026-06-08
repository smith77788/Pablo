from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from bot.utils.subscription import (
    BOT_LIMITS,
    PLAN_LEVELS,
    feature_required_plan,
    get_free_mode,
    normalize_plan,
    set_free_mode,
)


def test_max_alias_maps_to_enterprise() -> None:
    assert normalize_plan("max") == "enterprise"
    assert normalize_plan("maximum") == "enterprise"


def test_core_revenue_features_require_highest_plan() -> None:
    for feature in (
        "ai_assistant",
        "autonomous_engine",
        "global_presence",
        "swarm",
        "workspaces",
        "strike",
        "email_oauth",
        "infra_intelligence",
    ):
        assert feature_required_plan(feature) == "enterprise"


def test_free_plan_is_demo_not_full_product() -> None:
    assert BOT_LIMITS["free"] == 1
    assert feature_required_plan("basic_broadcast") == "starter"

    accounts_source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/accounts.py")
        .read_text(encoding="utf-8")
    )
    assert '"free": 0' in accounts_source
    assert '"starter": 1' in accounts_source
    assert '"pro": 3' in accounts_source
    assert "def _next_account_plan" in accounts_source


def test_global_free_mode_requires_explicit_env_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_GLOBAL_FREE_MODE", raising=False)
    set_free_mode(True)
    assert get_free_mode() is False

    monkeypatch.setenv("ALLOW_GLOBAL_FREE_MODE", "true")
    set_free_mode(True)
    assert get_free_mode() is True
    set_free_mode(False)


def test_revenue_entrypoints_have_plan_gates() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/botmother_menu.py")
        .read_text(encoding="utf-8")
    )

    for handler in (
        "async def cb_comms",
        "async def cb_broadcasts",
        "async def cb_inbox",
        "async def cb_ai_assistant",
    ):
        start = source.index(handler)
        body = source[start : source.index("@router.callback_query", start + 1)]
        assert "await require_plan(" in body
        assert "subscription_locked_markup(" in body


def test_unknown_features_default_to_enterprise() -> None:
    assert feature_required_plan("new_unclassified_feature") == "enterprise"


def test_plan_levels_are_monotonic() -> None:
    assert PLAN_LEVELS["free"] < PLAN_LEVELS["starter"]
    assert PLAN_LEVELS["starter"] < PLAN_LEVELS["pro"]
    assert PLAN_LEVELS["pro"] < PLAN_LEVELS["enterprise"]
