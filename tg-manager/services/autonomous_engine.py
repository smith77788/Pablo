"""Autonomous Operations Engine for BotMother Epoch V.

This module is the coordination layer above Intent Planner, Infrastructure
Orchestrator, and Operation Bus. It plans execution, resources, queue shape,
risk controls, and recovery steps before anything is submitted for execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg

from services import infra_orchestrator, intent_planner

Strategy = Literal["safest", "balanced", "fastest", "scalable"]
StrategyRequest = Strategy | Literal["auto"]


@dataclass(frozen=True)
class AutonomousContract:
    intent_type: str
    description: str
    strategy: Strategy
    plan: dict[str, Any]
    forecast: dict[str, Any]
    resource_plan: dict[str, Any]
    queue_plan: dict[str, Any]
    risk_plan: dict[str, Any]
    recovery_plan: dict[str, Any]
    execution_plan: list[str] = field(default_factory=list)

    def enriched_plan(self) -> dict[str, Any]:
        plan = dict(self.plan)
        plan["autonomous"] = {
            "version": "epoch_v",
            "strategy": self.strategy,
            "execution_plan": self.execution_plan,
            "resource_plan": self.resource_plan,
            "queue_plan": self.queue_plan,
            "risk_plan": self.risk_plan,
            "recovery_plan": self.recovery_plan,
        }
        plan["steps"] = self.execution_plan or plan.get("steps", [])

        # Promote non-executable plans to execute_op when the risk gate is green
        # and accounts are assigned — covers custom/free-text goals.
        if not plan.get("executable"):
            has_accounts = bool(self.resource_plan.get("primary_account_ids"))
            gate_go = bool(self.risk_plan.get("go", True)) and not self.risk_plan.get("blockers")
            if has_accounts and gate_go:
                plan["executable"] = True
                plan["action"] = "execute_op"

        return plan


async def build_autonomous_contract(
    pool: asyncpg.Pool,
    owner_id: int,
    description: str,
    requested_strategy: StrategyRequest = "auto",
) -> AutonomousContract:
    """Build a full execution contract from a user goal."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    intent_type = intent_planner.classify_intent(description)

    try:
        resources = await intent_planner.assess_resources(pool, owner_id)
    except Exception as exc:
        _log.warning("build_autonomous_contract: assess_resources failed: %s", exc)
        resources = {
            "accounts_available": 0,
            "accounts_avg_trust": 0.5,
            "proxies_available": 0,
            "active_operations": 0,
            "active_gp_plans": 0,
        }

    try:
        base_plan = await intent_planner.build_plan(
            pool, owner_id, intent_type, description, resources
        )
    except Exception as exc:
        _log.warning("build_autonomous_contract: build_plan failed: %s", exc)
        base_plan = {
            "intent_type": intent_type or "custom",
            "goal": description[:120],
            "n_accounts_available": resources.get("accounts_available", 0),
            "steps": ["1. Уточните цель — выберите тип намерения"],
            "risks": ["ℹ️ Не удалось загрузить данные инфраструктуры"],
            "executable": False,
            "action": "navigate",
            "navigate_to": "main",
        }
    infra_state = await _safe_infra_state(pool, owner_id)
    strategy = choose_strategy(base_plan, resources, infra_state, requested_strategy)
    forecast = intent_planner.forecast_execution(base_plan, strategy=strategy)
    resource_plan = await build_resource_plan(
        pool, owner_id, intent_type, base_plan, resources, strategy
    )
    queue_plan = build_queue_plan(base_plan, resources, infra_state, strategy)
    risk_plan = build_risk_plan(base_plan, resources, infra_state, forecast, strategy)
    recovery_plan = build_recovery_plan(base_plan, resource_plan, risk_plan)
    execution_plan = build_execution_plan(
        base_plan, resource_plan, queue_plan, risk_plan
    )
    forecast = enrich_forecast(forecast, risk_plan, queue_plan)

    return AutonomousContract(
        intent_type=intent_type,
        description=description,
        strategy=strategy,
        plan=base_plan,
        forecast=forecast,
        resource_plan=resource_plan,
        queue_plan=queue_plan,
        risk_plan=risk_plan,
        recovery_plan=recovery_plan,
        execution_plan=execution_plan,
    )


def choose_strategy(
    plan: dict[str, Any],
    resources: dict[str, Any],
    infra_state: dict[str, Any],
    requested_strategy: StrategyRequest = "auto",
) -> Strategy:
    if requested_strategy != "auto":
        return requested_strategy

    pressure = int(infra_state.get("pressure", {}).get("score", 0) or 0)
    active_ops = int(resources.get("active_operations", 0) or 0)
    accounts = int(
        plan.get("n_accounts_selected", resources.get("accounts_available", 0)) or 0
    )
    targets = _target_count(plan)
    intent_type = str(plan.get("intent_type", "custom"))

    if (
        pressure >= 70
        or active_ops >= 5
        or (_intent_requires_accounts(intent_type) and accounts <= 1)
    ):
        return "safest"
    if intent_type in {"audit", "sync", "visibility"}:
        return "balanced"
    if targets >= max(100, accounts * 35):
        return "scalable"
    return "balanced"


async def build_resource_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    intent_type: str,
    plan: dict[str, Any],
    resources: dict[str, Any],
    strategy: Strategy,
) -> dict[str, Any]:
    op_type = _intent_to_op_type(intent_type, plan)
    read_only = not _intent_requires_accounts(intent_type)
    requested_accounts = 0
    if not read_only:
        requested_accounts = int(
            plan.get("n_accounts_selected")
            or min(
                resources.get("accounts_available", 0),
                max(1, _target_count(plan) // 15),
            )
            or 0
        )
    accounts = await _safe_recommend_accounts(
        pool, owner_id, op_type, requested_accounts
    )
    primary = [
        a.get("id") for a in accounts[: max(1, min(len(accounts), requested_accounts))]
    ]
    secondary = [a.get("id") for a in accounts[len(primary) : len(primary) + 3]]

    return {
        "op_type": op_type,
        "operation_class": "read_only" if read_only else "active",
        "requested_accounts": requested_accounts,
        "primary_account_ids": [i for i in primary if i is not None],
        "secondary_account_ids": [i for i in secondary if i is not None],
        "backup_account_target": 2 if strategy in {"safest", "scalable"} else 1,
        "proxy_policy": _proxy_policy(resources, strategy),
        "worker_policy": _worker_policy(strategy),
    }


def build_queue_plan(
    plan: dict[str, Any],
    resources: dict[str, Any],
    infra_state: dict[str, Any],
    strategy: Strategy,
) -> dict[str, Any]:
    targets = _target_count(plan)
    pressure = int(infra_state.get("pressure", {}).get("score", 0) or 0)
    active_ops = int(resources.get("active_operations", 0) or 0)
    parallelism = {"safest": 1, "balanced": 2, "fastest": 3, "scalable": 4}[strategy]
    if pressure >= 70 or active_ops >= 5:
        parallelism = 1
    batch_size = max(1, min(25, targets // max(1, parallelism * 2)))

    return {
        "parallelism": parallelism,
        "batch_size": batch_size,
        "defer_when_pressure_above": 85,
        "active_operations": active_ops,
        "monitoring_interval_seconds": 45 if strategy == "fastest" else 90,
    }


def build_risk_plan(
    plan: dict[str, Any],
    resources: dict[str, Any],
    infra_state: dict[str, Any],
    forecast: dict[str, Any],
    strategy: Strategy,
) -> dict[str, Any]:
    pressure = int(infra_state.get("pressure", {}).get("score", 0) or 0)
    accounts = int(resources.get("accounts_available", 0) or 0)
    risk_score = float(forecast.get("risk_score", 0.2) or 0.2)
    blockers: list[str] = []
    warnings: list[str] = []

    if (
        _intent_requires_accounts(str(plan.get("intent_type", "custom")))
        and accounts <= 0
    ):
        blockers.append("No available accounts")
    if pressure >= 85:
        blockers.append("Infrastructure pressure is too high")
    elif pressure >= 70:
        warnings.append("Infrastructure pressure is elevated")
    if strategy == "fastest" and risk_score >= 0.35:
        warnings.append("Fast strategy increases rate-limit risk")
    if plan.get("intent_type") == "strike":
        warnings.append("Strike requires explicit owner confirmation and lawful use")

    return {
        "score": round(min(1.0, risk_score + (0.12 if pressure >= 70 else 0.0)), 2),
        "blockers": blockers,
        "warnings": warnings,
        "go": not blockers,
        "pressure_score": pressure,
    }


def build_recovery_plan(
    plan: dict[str, Any],
    resource_plan: dict[str, Any],
    risk_plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "backup_accounts": resource_plan.get("secondary_account_ids", []),
        "on_flood_wait": "pause_account_and_shift_batch",
        "on_dead_session": "deactivate_session_and_replace_from_backup",
        "on_high_pressure": "pause_queue_until_pressure_recovers",
        "on_partial_failure": "retry_failed_items_only",
        "manual_review_required": bool(risk_plan.get("blockers")),
        "post_run_learning": [
            "record_account_outcomes",
            "record_proxy_outcomes",
            "compare_forecast_to_actual",
        ],
    }


def build_execution_plan(
    plan: dict[str, Any],
    resource_plan: dict[str, Any],
    queue_plan: dict[str, Any],
    risk_plan: dict[str, Any],
) -> list[str]:
    return [
        f"1. Validate goal and operation type: {resource_plan['op_type']}",
        f"2. Assign primary accounts: {len(resource_plan['primary_account_ids'])}",
        f"3. Reserve backup accounts: {len(resource_plan['secondary_account_ids'])}",
        f"4. Build queue: parallelism {queue_plan['parallelism']}, batch {queue_plan['batch_size']}",
        f"5. Apply risk gate: {'go' if risk_plan['go'] else 'manual review'}",
        "6. Monitor execution and record learning after completion",
    ]


def enrich_forecast(
    forecast: dict[str, Any],
    risk_plan: dict[str, Any],
    queue_plan: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(forecast)
    if risk_plan.get("blockers"):
        enriched["success_probability"] = min(
            float(enriched.get("success_probability", 0.0) or 0.0), 0.25
        )
    enriched["risk_score"] = max(
        float(enriched.get("risk_score", 0.0) or 0.0),
        float(risk_plan.get("score", 0.0) or 0.0),
    )
    enriched["queue_parallelism"] = queue_plan.get("parallelism", 1)
    enriched["go"] = risk_plan.get("go", True)
    return enriched


def execution_gate(
    plan: dict[str, Any], forecast: dict[str, Any] | None = None
) -> dict[str, Any]:
    autonomous = plan.get("autonomous") or {}
    risk_plan = autonomous.get("risk_plan") or {}
    blockers = list(risk_plan.get("blockers") or [])
    warnings = list(risk_plan.get("warnings") or [])
    forecast_go = True if forecast is None else bool(forecast.get("go", True))
    go = bool(risk_plan.get("go", True)) and forecast_go and not blockers

    return {
        "go": go,
        "blockers": blockers,
        "warnings": warnings,
        "manual_review_required": not go,
    }


def format_autonomous_block(
    plan: dict[str, Any],
    strategy: str | None = None,
    forecast: dict[str, Any] | None = None,
) -> str:
    autonomous = plan.get("autonomous") or {}
    resource_plan = autonomous.get("resource_plan") or {}
    queue_plan = autonomous.get("queue_plan") or {}
    risk_plan = autonomous.get("risk_plan") or {}
    recovery_plan = autonomous.get("recovery_plan") or {}
    display_strategy = strategy or autonomous.get("strategy", "balanced")
    display_risk = risk_plan.get("score")
    if display_risk is None and forecast:
        display_risk = forecast.get("risk_score", 0.0)

    lines = [
        "<b>Autonomous Ops</b>",
        f"Strategy: <code>{display_strategy}</code>",
        f"Accounts: {len(resource_plan.get('primary_account_ids', []))} primary"
        f" + {len(resource_plan.get('secondary_account_ids', []))} backup",
        f"Queue: {queue_plan.get('parallelism', 1)} parallel,"
        f" batch {queue_plan.get('batch_size', 1)}",
        f"Risk: {int(float(display_risk or 0.0) * 100)}%",
    ]
    blockers = risk_plan.get("blockers") or []
    warnings = risk_plan.get("warnings") or []
    if blockers:
        lines.append("Blockers: " + "; ".join(blockers[:2]))
    elif warnings:
        lines.append("Warnings: " + "; ".join(warnings[:2]))
    lines.append(
        "Recovery: "
        + str(recovery_plan.get("on_partial_failure", "retry_failed_items_only"))
    )
    return "\n".join(lines)


async def _safe_infra_state(pool: asyncpg.Pool, owner_id: int) -> dict[str, Any]:
    try:
        return (await infra_orchestrator.get_state(pool, owner_id)).to_dict()
    except Exception:
        return {
            "pressure": {"score": 0, "label": "unknown"},
            "accounts": {"total": 0, "available": 0, "cooling": 0},
            "queue": {"pending": 0, "running": 0},
            "is_healthy": True,
            "recommendations": [],
        }


async def _safe_recommend_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    count: int,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    try:
        rows = await infra_orchestrator.recommend_accounts(
            pool, owner_id, op_type, count=count
        )
        return [dict(row) for row in rows]
    except Exception:
        return []


def _intent_to_op_type(intent_type: str, plan: dict[str, Any]) -> str:
    if intent_type == "presence":
        asset_type = str(plan.get("asset_type", "channel"))
        return {
            "channel": "global_presence_channel",
            "group": "global_presence_group",
            "bot": "global_presence_bot",
            "package": "global_presence_package",
            "full_package": "global_presence_full_package",
        }.get(asset_type, "global_presence_channel")
    if intent_type == "strike":
        return "strike"
    if intent_type == "audit":
        return "infra_audit"
    if intent_type == "sync":
        return "bulk_bot_edit"
    if intent_type == "visibility":
        return "visibility_audit"
    # For custom goals, try to infer op_type from the description keywords
    goal = (plan.get("goal") or "").lower()
    if any(kw in goal for kw in ("создать канал", "создай канал", "создать каналы", "создай каналы",
                                  "create channel", "пустых канал", "новый канал")):
        return "bulk_create_channels"
    if any(kw in goal for kw in ("вступить", "вступи ", "подписаться", "join channel",
                                  "bulk join", "массово вступ")):
        return "bulk_join"
    if any(kw in goal for kw in ("покинуть", "покинь", "выйти", "выйди", "leave channel",
                                  "bulk leave", "массово покин")):
        return "bulk_leave"
    return "mass_publish"


def _intent_requires_accounts(intent_type: str) -> bool:
    return intent_type not in {"audit", "visibility"}


def _target_count(plan: dict[str, Any]) -> int:
    for key in ("n_targets", "n_total", "n_keywords", "n_channels", "n_bots"):
        value = plan.get(key)
        if isinstance(value, int | float) and value > 0:
            return int(value)
    return 1


def _proxy_policy(resources: dict[str, Any], strategy: Strategy) -> dict[str, Any]:
    available = int(resources.get("proxies_available", 0) or 0)
    return {
        "required": strategy in {"safest", "scalable"},
        "available": available,
        "rotation": "per_batch" if strategy in {"safest", "scalable"} else "sticky",
        "fallback": "account_without_proxy_allowed" if available == 0 else "rotate",
    }


def _worker_policy(strategy: Strategy) -> dict[str, Any]:
    return {
        "mode": strategy,
        "max_parallel_workers": {
            "safest": 1,
            "balanced": 2,
            "fastest": 3,
            "scalable": 4,
        }[strategy],
        "cooldown_policy": "strict" if strategy in {"safest", "scalable"} else "normal",
    }
