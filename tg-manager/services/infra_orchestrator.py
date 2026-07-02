"""
Infrastructure Orchestrator — центральный мозг инфраструктуры Infragram.

Единая точка входа для всех инфраструктурных запросов:
  - get_state()            → полный снимок состояния инфраструктуры
  - recommend_accounts()   → лучшие аккаунты для операции (через resource_selector + infra_memory)
  - estimate_capacity()    → прогноз выполнения операции (через capacity_planner)
  - is_ready_for_op()      → безопасно ли запускать операцию сейчас

Координирует: infra_pressure, infra_advisor, capacity_planner, resource_selector, infra_memory.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)

# Порог давления выше которого новые операции блокируются по умолчанию
_PRESSURE_HARD_LIMIT = 85
# Порог для мягкого предупреждения (операция разрешена, но показывается тост)
_PRESSURE_SOFT_WARN = 70


@dataclass
class InfraState:
    """Снимок состояния инфраструктуры."""

    owner_id: int
    pressure_score: int = 0
    pressure_emoji: str = "🟢"
    pressure_label: str = "Норма"
    account_total: int = 0
    account_available: int = 0
    account_cooling: int = 0
    queue_pending: int = 0
    queue_running: int = 0
    recommendations: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.pressure_score < _PRESSURE_HARD_LIMIT

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "pressure": {
                "score": self.pressure_score,
                "emoji": self.pressure_emoji,
                "label": self.pressure_label,
            },
            "accounts": {
                "total": self.account_total,
                "available": self.account_available,
                "cooling": self.account_cooling,
            },
            "queue": {
                "pending": self.queue_pending,
                "running": self.queue_running,
            },
            "is_healthy": self.is_healthy,
            "recommendations": self.recommendations,
        }


async def get_state(pool: asyncpg.Pool, owner_id: int) -> InfraState:
    """Полный снимок состояния инфраструктуры для owner_id.

    Запускает все sub-запросы параллельно. Устойчив к частичным сбоям:
    любой упавший компонент не блокирует остальные — данные будут нулевые.
    """

    pressure_task = asyncio.create_task(_safe_pressure(pool, owner_id))
    recs_task = asyncio.create_task(_safe_recommendations(pool, owner_id))
    accs_task = asyncio.create_task(_safe_account_counts(pool, owner_id))
    queue_task = asyncio.create_task(_safe_queue_counts(pool, owner_id))

    pressure, recs, accs, queue = await asyncio.gather(
        pressure_task, recs_task, accs_task, queue_task
    )

    return InfraState(
        owner_id=owner_id,
        pressure_score=pressure.get("score", 0),
        pressure_emoji=pressure.get("level_emoji", "🟢"),
        pressure_label=pressure.get("level_label", "Норма"),
        account_total=accs.get("total", 0),
        account_available=accs.get("available", 0),
        account_cooling=accs.get("cooling", 0),
        queue_pending=queue.get("pending", 0),
        queue_running=queue.get("running", 0),
        recommendations=recs,
        extra={"pressure_breakdown": pressure.get("breakdown", {})},
    )


async def recommend_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str,
    count: int = 10,
    *,
    pool_name: Optional[str] = None,
    tags: Optional[list[str]] = None,
    include_ids: Optional[list[int]] = None,
) -> list[dict]:
    """Вернуть count лучших аккаунтов для операции action_type.

    Использует resource_selector (flood-aware) плюс infra_memory scoring
    для дополнительного ранжирования по историческому опыту.
    """
    from services import resource_selector, infra_memory

    if include_ids:
        candidates = await resource_selector.select_all_active(
            pool,
            owner_id,
            include_ids=include_ids,
            pool_name=pool_name,
            tags=tags,
        )
        scored = infra_memory.rank_accounts_by_memory(
            [a["id"] for a in candidates], action_type
        )
        score_map = {acc_id: score for acc_id, score in scored}
        cands_by_id = {a["id"]: dict(a) for a in candidates}
        sorted_ids = sorted(
            cands_by_id.keys(), key=lambda i: score_map.get(i, 0.5), reverse=True
        )
        return [cands_by_id[i] for i in sorted_ids[:count]]

    if count == 1:
        acc = await resource_selector.select_account(
            pool, owner_id, action_type, pool_name=pool_name, tags=tags
        )
        return [acc] if acc else []

    return await resource_selector.select_accounts(
        pool, owner_id, count, action_type, pool_name=pool_name, tags=tags
    )


async def estimate_capacity(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    total_items: int,
    account_ids: Optional[list[int]] = None,
) -> dict:
    """Прогноз выполнения операции.

    Возвращает dict с ключами:
      account_count, estimated_minutes, items_per_account, warnings,
      safe_to_start (bool), summary_text
    """
    from services.capacity_planner import plan_operation

    try:
        plan = await plan_operation(pool, owner_id, op_type, total_items, account_ids)
        return {
            "account_count": plan.account_count,
            "estimated_minutes": plan.estimated_minutes,
            "items_per_account": plan.items_per_account,
            "warnings": plan.warnings,
            "safe_to_start": plan.account_count > 0,
            "summary_text": plan.summary_text,
        }
    except Exception as e:
        log.warning(
            "infra_orchestrator.estimate_capacity failed owner=%d op=%s: %s",
            owner_id,
            op_type,
            e,
        )
        return {
            "account_count": 0,
            "estimated_minutes": 0,
            "items_per_account": 0,
            "warnings": [str(e)],
            "safe_to_start": False,
            "summary_text": "Оценка недоступна",
        }


async def is_ready_for_op(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str = "default",
    *,
    pressure_limit: int = _PRESSURE_HARD_LIMIT,
) -> tuple[bool, str]:
    """Проверить, безопасно ли запускать операцию сейчас.

    Возвращает (True, "") или (False, reason_text).
    Быстрая проверка — только давление + наличие доступных аккаунтов.
    """

    try:
        pressure = await _safe_pressure(pool, owner_id)
        score = pressure.get("score", 0)
        if score >= pressure_limit:
            label = pressure.get("level_label", "Высокая")
            return (
                False,
                f"Давление инфраструктуры {label} ({score}/100) — операция заблокирована",
            )
    except Exception as e:
        log.debug("infra_orchestrator.is_ready_for_op pressure check failed: %s", e)

    try:
        accs = await _safe_account_counts(pool, owner_id)
        if accs.get("available", 0) == 0:
            return False, "Нет доступных аккаунтов (все на cooldown или неактивны)"
    except Exception as e:
        log.debug("infra_orchestrator.is_ready_for_op account check failed: %s", e)

    return True, ""


async def get_pressure_warning(
    pool: asyncpg.Pool,
    owner_id: int,
    warn_threshold: int = _PRESSURE_SOFT_WARN,
) -> str | None:
    """Мягкое предупреждение: warn_threshold ≤ давление < HARD_LIMIT.

    Возвращает текст предупреждения или None.  Хардблок — через is_ready_for_op().
    """
    try:
        pressure = await _safe_pressure(pool, owner_id)
        score = pressure.get("score", 0)
        if warn_threshold <= score < _PRESSURE_HARD_LIMIT:
            label = pressure.get("level_label", "Повышенное")
            return f"⚠️ Давление инфраструктуры {label} ({score}/100) — операция продолжается"
    except Exception:
        pass
    return None


# ── Private helpers ─────────────────────────────────────────────────────────


async def _safe_pressure(pool: asyncpg.Pool, owner_id: int) -> dict:
    try:
        from services.infra_pressure import compute_pressure

        return await compute_pressure(pool, owner_id)
    except Exception as e:
        log.debug("infra_orchestrator: pressure query failed owner=%d: %s", owner_id, e)
        return {
            "score": 0,
            "level_emoji": "🟢",
            "level_label": "Норма",
            "breakdown": {},
        }


async def _safe_recommendations(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    try:
        from services.infra_advisor import get_recommendations

        return await get_recommendations(pool, owner_id)
    except Exception as e:
        log.debug(
            "infra_orchestrator: recommendations failed owner=%d: %s", owner_id, e
        )
        return []


async def _safe_account_counts(pool: asyncpg.Pool, owner_id: int) -> dict:
    try:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE is_active) AS total,
                   COUNT(*) FILTER (WHERE is_active AND (cooldown_until IS NULL OR cooldown_until < NOW())) AS available,
                   COUNT(*) FILTER (WHERE is_active AND cooldown_until > NOW()) AS cooling
               FROM tg_accounts WHERE owner_id=$1""",
            owner_id,
        )
        return dict(row) if row else {}
    except Exception as e:
        log.debug("infra_orchestrator: account counts failed owner=%d: %s", owner_id, e)
        return {}


async def _safe_queue_counts(pool: asyncpg.Pool, owner_id: int) -> dict:
    try:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='pending') AS pending,
                   COUNT(*) FILTER (WHERE status='running') AS running
               FROM operation_queue WHERE owner_id=$1""",
            owner_id,
        )
        return dict(row) if row else {}
    except Exception as e:
        log.debug("infra_orchestrator: queue counts failed owner=%d: %s", owner_id, e)
        return {}


async def get_full_intelligence(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
    account_ids: Optional[list[int]] = None,
) -> dict:
    """Единая точка входа для полного pre-launch intelligence (через intelligence_engine).

    Возвращает dict с ключами:
      go (bool), reason (str), warning (str),
      risk_level (str), risk_score (int),
      pressure (int), accounts_available (int),
      prediction_minutes (int), success_probability (float),
      formatted_block (str)  — готовый HTML для Telegram
    """
    try:
        from services.intelligence_engine import (
            get_pre_launch_intelligence,
            format_pre_launch_block,
        )

        intel = await get_pre_launch_intelligence(
            pool, owner_id, op_type, item_count, account_ids
        )
        return {
            "go": intel.go_decision,
            "reason": intel.go_reason,
            "warning": intel.warning_text,
            "risk_level": intel.risk.level,
            "risk_score": intel.risk.score,
            "pressure": intel.pressure_score,
            "accounts_available": len(intel.recommended_accounts),
            "prediction_minutes": intel.prediction.estimated_minutes,
            "success_probability": intel.prediction.success_probability,
            "formatted_block": format_pre_launch_block(intel),
        }
    except Exception as e:
        log.warning(
            "infra_orchestrator.get_full_intelligence failed owner=%d op=%s: %s",
            owner_id,
            op_type,
            e,
        )
        return {
            "go": True,
            "reason": "",
            "warning": "",
            "risk_level": "unknown",
            "risk_score": 0,
            "pressure": 0,
            "accounts_available": 0,
            "prediction_minutes": 0,
            "success_probability": 0.8,
            "formatted_block": "",
        }
