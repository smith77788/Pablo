"""
Capacity Planner — прогнозирование нагрузки и безопасного тайминга операций.

Перед запуском bulk-операций рассчитывает:
- Сколько операций в секунду безопасно для каждого аккаунта
- Общую ожидаемую длительность
- Риск флуд-вейта по текущим данным
- Рекомендуемое количество аккаунтов
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
from services.flood_engine import normalize_trust_score

log = logging.getLogger(__name__)

# Telegram soft limits (conservative estimates)
_SAFE_JOIN_PER_HOUR = 15  # вступлений в час на аккаунт
_SAFE_POST_PER_HOUR = 20  # публикаций в час на аккаунт
_SAFE_EDIT_PER_HOUR = 30  # редактирований в час на аккаунт
_SAFE_DM_PER_HOUR = 30  # DM в час на аккаунт

_OP_LIMITS: dict[str, int] = {
    "join": _SAFE_JOIN_PER_HOUR,
    "leave": _SAFE_JOIN_PER_HOUR,
    "post": _SAFE_POST_PER_HOUR,
    "edit": _SAFE_EDIT_PER_HOUR,
    "dm": _SAFE_DM_PER_HOUR,
}


@dataclass
class CapacityPlan:
    op_type: str
    total_items: int
    account_count: int
    estimated_minutes: float
    items_per_account: int
    risk_level: str  # low / medium / high
    warnings: list[str] = field(default_factory=list)
    recommended_accounts: int = 1

    @property
    def estimated_hours(self) -> float:
        return self.estimated_minutes / 60

    def summary_text(self) -> str:
        risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
            self.risk_level, "⚪"
        )
        lines = [
            "📊 <b>Прогноз операции</b>",
            "",
            f"Тип: <b>{self.op_type}</b>",
            f"Элементов: <b>{self.total_items}</b>",
            f"Аккаунтов: <b>{self.account_count}</b>",
            "",
            f"⏱️ Ожидаемое время: <b>~{self.estimated_minutes:.0f} мин</b>",
            f"📦 На аккаунт: ~{self.items_per_account} элементов",
            f"{risk_emoji} Риск: <b>{self.risk_level}</b>",
        ]
        if self.warnings:
            lines.append("")
            lines.append("⚠️ <b>Предупреждения:</b>")
            for w in self.warnings:
                lines.append(f"• {w}")
        if self.recommended_accounts > self.account_count:
            lines.append("")
            lines.append(
                f"💡 <i>Рекомендуется {self.recommended_accounts} аккаунтов "
                f"для снижения риска</i>"
            )
        return "\n".join(lines)


async def plan_operation(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    total_items: int,
    account_ids: Optional[list[int]] = None,
) -> CapacityPlan:
    """
    Рассчитать план выполнения операции.
    op_type: 'join' | 'leave' | 'post' | 'edit' | 'dm'
    """
    safe_per_hour = _OP_LIMITS.get(op_type, 20)
    warnings = []

    # Получить аккаунты
    if account_ids:
        accounts = await pool.fetch(
            """SELECT id, trust_score, flood_count_7d, cooldown_until
               FROM tg_accounts
               WHERE id = ANY($1) AND owner_id = $2 AND is_active = true""",
            account_ids,
            owner_id,
        )
    else:
        accounts = await pool.fetch(
            """SELECT id, trust_score, flood_count_7d, cooldown_until
               FROM tg_accounts
               WHERE owner_id = $1 AND is_active = true
               ORDER BY trust_score DESC NULLS LAST""",
            owner_id,
        )

    account_count = len(accounts)
    if account_count == 0:
        return CapacityPlan(
            op_type=op_type,
            total_items=total_items,
            account_count=0,
            estimated_minutes=0,
            items_per_account=0,
            risk_level="high",
            warnings=["Нет активных аккаунтов"],
        )

    # Аккаунты на кулдауне
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    cooling = [
        a for a in accounts if a.get("cooldown_until") and a["cooldown_until"] > now
    ]
    if cooling:
        warnings.append(f"{len(cooling)} аккаунт(ов) на кулдауне")

    available = account_count - len(cooling)
    if available <= 0:
        return CapacityPlan(
            op_type=op_type,
            total_items=total_items,
            account_count=account_count,
            estimated_minutes=999,
            items_per_account=0,
            risk_level="high",
            warnings=["Все аккаунты на кулдауне"],
        )

    # Аккаунты с высоким flood_count_7d
    high_flood = [a for a in accounts if (a.get("flood_count_7d") or 0) >= 5]
    if high_flood:
        warnings.append(f"{len(high_flood)} аккаунт(ов) с высокой частотой флуд-вейтов")

    # Снизить effective rate для аккаунтов с флудами
    avg_trust = sum(
        normalize_trust_score(a.get("trust_score")) or 0.5 for a in accounts
    ) / max(1, account_count)
    trust_factor = avg_trust  # canonical 0.0 - 1.0

    effective_per_hour = safe_per_hour * (0.5 + 0.5 * trust_factor)
    total_capacity_per_hour = effective_per_hour * available

    # Время выполнения в минутах
    estimated_minutes = (total_items / max(1, total_capacity_per_hour)) * 60
    items_per_account = total_items // max(1, available)

    # Рекомендованное количество аккаунтов
    recommended = max(1, (total_items // safe_per_hour) + 1)

    # Оценка риска
    ratio = total_items / max(1, total_capacity_per_hour)
    if ratio > 3:
        risk_level = "high"
        warnings.append("Очень высокая нагрузка на аккаунты — высокий риск флуд-вейтов")
    elif ratio > 1.5:
        risk_level = "medium"
        warnings.append("Умеренная нагрузка — рекомендуется добавить аккаунты")
    else:
        risk_level = "low"

    if estimated_minutes > 60 * 8:
        warnings.append("Операция займёт более 8 часов")

    return CapacityPlan(
        op_type=op_type,
        total_items=total_items,
        account_count=account_count,
        estimated_minutes=estimated_minutes,
        items_per_account=items_per_account,
        risk_level=risk_level,
        warnings=warnings,
        recommended_accounts=recommended,
    )
