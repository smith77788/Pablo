from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from bot.utils.subscription import (
    PLAN_LEVELS,
    feature_required_plan,
    normalize_plan,
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


def test_unknown_features_default_to_enterprise() -> None:
    assert feature_required_plan("new_unclassified_feature") == "enterprise"


def test_plan_levels_are_monotonic() -> None:
    assert PLAN_LEVELS["free"] < PLAN_LEVELS["starter"]
    assert PLAN_LEVELS["starter"] < PLAN_LEVELS["pro"]
    assert PLAN_LEVELS["pro"] < PLAN_LEVELS["enterprise"]
