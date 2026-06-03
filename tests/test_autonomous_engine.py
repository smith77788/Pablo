from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost/test")
os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from services.autonomous_engine import (
    build_queue_plan,
    build_risk_plan,
    choose_strategy,
    enrich_forecast,
    format_autonomous_block,
)


def test_choose_strategy_uses_safest_under_pressure() -> None:
    plan = {"intent_type": "presence", "n_targets": 180, "n_accounts_selected": 8}
    resources = {"accounts_available": 8, "active_operations": 1}
    infra_state = {"pressure": {"score": 91}}

    assert choose_strategy(plan, resources, infra_state) == "safest"


def test_choose_strategy_uses_scalable_for_large_target_sets() -> None:
    plan = {"intent_type": "presence", "n_targets": 220, "n_accounts_selected": 4}
    resources = {"accounts_available": 4, "active_operations": 0}
    infra_state = {"pressure": {"score": 20}}

    assert choose_strategy(plan, resources, infra_state) == "scalable"


def test_choose_strategy_keeps_visibility_balanced_without_accounts() -> None:
    plan = {"intent_type": "visibility", "n_keywords": 25}
    resources = {"accounts_available": 0, "active_operations": 0}
    infra_state = {"pressure": {"score": 20}}

    assert choose_strategy(plan, resources, infra_state) == "balanced"


def test_queue_plan_clamps_parallelism_when_system_is_hot() -> None:
    plan = {"n_targets": 100}
    resources = {"active_operations": 6}
    infra_state = {"pressure": {"score": 74}}

    queue_plan = build_queue_plan(plan, resources, infra_state, "scalable")

    assert queue_plan["parallelism"] == 1
    assert queue_plan["batch_size"] == 25


def test_queue_plan_uses_visibility_keyword_count() -> None:
    plan = {"intent_type": "visibility", "n_keywords": 40}
    resources = {"active_operations": 0}
    infra_state = {"pressure": {"score": 10}}

    queue_plan = build_queue_plan(plan, resources, infra_state, "balanced")

    assert queue_plan["parallelism"] == 2
    assert queue_plan["batch_size"] == 10


def test_risk_plan_blocks_when_no_accounts_are_available() -> None:
    plan = {"intent_type": "presence"}
    resources = {"accounts_available": 0}
    infra_state = {"pressure": {"score": 30}}
    forecast = {"risk_score": 0.2}

    risk_plan = build_risk_plan(plan, resources, infra_state, forecast, "balanced")

    assert risk_plan["go"] is False
    assert "No available accounts" in risk_plan["blockers"]


def test_risk_plan_allows_read_only_visibility_without_accounts() -> None:
    plan = {"intent_type": "visibility", "n_keywords": 12}
    resources = {"accounts_available": 0}
    infra_state = {"pressure": {"score": 30}}
    forecast = {"risk_score": 0.12}

    risk_plan = build_risk_plan(plan, resources, infra_state, forecast, "balanced")

    assert risk_plan["go"] is True
    assert risk_plan["blockers"] == []


def test_enrich_forecast_marks_blocked_contract_as_no_go() -> None:
    forecast = {"risk_score": 0.2, "success_probability": 0.9}
    risk_plan = {"score": 0.4, "go": False, "blockers": ["No available accounts"]}
    queue_plan = {"parallelism": 2}

    enriched = enrich_forecast(forecast, risk_plan, queue_plan)

    assert enriched["go"] is False
    assert enriched["success_probability"] == 0.25
    assert enriched["queue_parallelism"] == 2


def test_format_autonomous_block_exposes_operational_contract() -> None:
    plan = {
        "autonomous": {
            "strategy": "balanced",
            "resource_plan": {
                "primary_account_ids": [1, 2],
                "secondary_account_ids": [3],
            },
            "queue_plan": {"parallelism": 2, "batch_size": 10},
            "risk_plan": {"score": 0.22, "warnings": ["Elevated pressure"]},
            "recovery_plan": {"on_partial_failure": "retry_failed_items_only"},
        }
    }

    text = format_autonomous_block(plan, strategy="safest")

    assert "Autonomous Ops" in text
    assert "<code>safest</code>" in text
    assert "2 primary + 1 backup" in text
    assert "Risk: 22%" in text
