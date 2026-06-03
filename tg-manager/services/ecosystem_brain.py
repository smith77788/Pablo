"""Ecosystem Brain — центральный мозг экосистем BotMother.

BOTMOTHER ЭПОХА III: Ecosystem Brain Contract

Экосистема — живой объект с собственным состоянием, памятью, метриками и рисками.
Ecosystem Brain вычисляет:
  - Health Score (здоровье аккаунтов, прокси, операций)
  - Pressure Score (нагрузка, плотность, перегруженность)
  - Risk Assessment (operational / infrastructure / account / proxy / recovery)
  - Drift Detection (отклонения от шаблонов)
  - Memory (история операций, изменений, ошибок)
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class EcosystemHealth:
    health_score: float = 1.0        # 0.0–1.0
    stability_score: float = 1.0
    reliability_score: float = 1.0
    recovery_score: float = 1.0
    growth_score: float = 0.0
    account_count: int = 0
    healthy_accounts: int = 0
    active_proxies: int = 0
    healthy_proxies: int = 0
    recent_op_success_rate: float = 1.0
    restrictions_count: int = 0

    @property
    def overall(self) -> float:
        return round(
            self.health_score * 0.35
            + self.stability_score * 0.25
            + self.reliability_score * 0.25
            + self.recovery_score * 0.15,
            3,
        )

    @property
    def grade(self) -> str:
        s = self.overall
        if s >= 0.85:
            return "🟢 Отличное"
        if s >= 0.65:
            return "🟡 Хорошее"
        if s >= 0.40:
            return "🟠 Ослабленное"
        return "🔴 Критическое"


@dataclass
class EcosystemPressure:
    score: int = 0          # 0–100
    account_load: float = 0.0
    operation_density: float = 0.0
    cooldown_ratio: float = 0.0
    active_tasks: int = 0
    overloaded_accounts: int = 0
    overloaded_proxies: int = 0

    @property
    def level(self) -> str:
        if self.score >= 85:
            return "🔴 Критическое"
        if self.score >= 70:
            return "🟠 Высокое"
        if self.score >= 40:
            return "🟡 Умеренное"
        return "🟢 Низкое"

    @property
    def emoji(self) -> str:
        if self.score >= 85:
            return "🔴"
        if self.score >= 70:
            return "🟠"
        if self.score >= 40:
            return "🟡"
        return "🟢"


@dataclass
class EcosystemRisk:
    operational_risk: float = 0.0   # 0.0–1.0
    infrastructure_risk: float = 0.0
    account_risk: float = 0.0
    proxy_risk: float = 0.0
    recovery_risk: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def overall(self) -> float:
        return round(
            self.operational_risk * 0.30
            + self.infrastructure_risk * 0.25
            + self.account_risk * 0.25
            + self.proxy_risk * 0.10
            + self.recovery_risk * 0.10,
            3,
        )

    @property
    def level(self) -> str:
        s = self.overall
        if s >= 0.75:
            return "critical"
        if s >= 0.50:
            return "high"
        if s >= 0.25:
            return "medium"
        return "low"

    @property
    def level_label(self) -> str:
        labels = {"critical": "🚨 Критический", "high": "🔴 Высокий",
                  "medium": "🟡 Средний", "low": "🟢 Низкий"}
        return labels.get(self.level, "🟢 Низкий")


@dataclass
class EcosystemDrift:
    drift_type: str
    object_type: Optional[str]
    object_id: Optional[int]
    description: str
    suggested_fix: Optional[str] = None
    auto_fixable: bool = False


@dataclass
class EcosystemSnapshot:
    ecosystem_id: int
    name: str
    ecosystem_type: str
    status: str
    health: EcosystemHealth
    pressure: EcosystemPressure
    risk: EcosystemRisk
    drifts: list[EcosystemDrift] = field(default_factory=list)
    member_counts: dict[str, int] = field(default_factory=dict)
    recent_events: int = 0


# ── DB helpers ────────────────────────────────────────────────────────────────

async def create_ecosystem(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    description: str = "",
    ecosystem_type: str = "custom",
    region: Optional[str] = None,
) -> int:
    """Создаёт новую экосистему. Возвращает id."""
    row = await pool.fetchrow(
        """INSERT INTO ecosystems (owner_id, name, description, ecosystem_type, region)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id""",
        owner_id, name, description, ecosystem_type, region,
    )
    return row["id"]


async def get_ecosystem(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> Optional[dict]:
    return await pool.fetchrow(
        "SELECT * FROM ecosystems WHERE id=$1 AND owner_id=$2",
        ecosystem_id, owner_id,
    )


async def list_ecosystems(
    pool: asyncpg.Pool, owner_id: int, status: str = "active"
) -> list[dict]:
    return await pool.fetch(
        """SELECT e.*,
                  (SELECT COUNT(*) FROM ecosystem_members m WHERE m.ecosystem_id=e.id) AS member_count
           FROM ecosystems e
           WHERE e.owner_id=$1 AND e.status=$2
           ORDER BY e.updated_at DESC""",
        owner_id, status,
    )


async def add_member(
    pool: asyncpg.Pool,
    ecosystem_id: int,
    owner_id: int,
    object_type: str,
    object_id: int,
    role: str = "member",
) -> bool:
    """Добавляет объект в экосистему. True если добавлен, False если уже был."""
    try:
        await pool.execute(
            """INSERT INTO ecosystem_members (ecosystem_id, owner_id, object_type, object_id, role)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (ecosystem_id, object_type, object_id) DO NOTHING""",
            ecosystem_id, owner_id, object_type, object_id, role,
        )
        return True
    except Exception:
        return False


async def remove_member(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int,
    object_type: str, object_id: int
) -> None:
    await pool.execute(
        """DELETE FROM ecosystem_members
           WHERE ecosystem_id=$1 AND owner_id=$2
             AND object_type=$3 AND object_id=$4""",
        ecosystem_id, owner_id, object_type, object_id,
    )


async def get_members(
    pool: asyncpg.Pool, ecosystem_id: int, object_type: Optional[str] = None
) -> list[dict]:
    if object_type:
        return await pool.fetch(
            "SELECT * FROM ecosystem_members WHERE ecosystem_id=$1 AND object_type=$2 ORDER BY added_at",
            ecosystem_id, object_type,
        )
    return await pool.fetch(
        "SELECT * FROM ecosystem_members WHERE ecosystem_id=$1 ORDER BY object_type, added_at",
        ecosystem_id,
    )


async def record_event(
    pool: asyncpg.Pool,
    ecosystem_id: int,
    owner_id: int,
    event_type: str,
    title: str,
    severity: str = "info",
    details: Optional[dict] = None,
    object_type: Optional[str] = None,
    object_id: Optional[int] = None,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO ecosystem_events
               (ecosystem_id, owner_id, event_type, severity, title, details, object_type, object_id)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)""",
            ecosystem_id, owner_id, event_type, severity, title,
            __import__("json").dumps(details or {}),
            object_type, object_id,
        )
    except Exception as e:
        log.debug("ecosystem record_event: %s", e)


async def delete_ecosystem(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> None:
    await pool.execute(
        "UPDATE ecosystems SET status='archived', updated_at=NOW() WHERE id=$1 AND owner_id=$2",
        ecosystem_id, owner_id,
    )


# ── Health computation ────────────────────────────────────────────────────────

async def compute_health(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> EcosystemHealth:
    """Вычисляет здоровье экосистемы из состояния её участников."""
    h = EcosystemHealth()
    try:
        # Accounts: health from tg_accounts + cooldown state
        acc_rows = await pool.fetch(
            """SELECT a.trust_score, a.is_active,
                      a.cooldown_until, a.flood_count_7d,
                      COALESCE(ah.health_score, 0.7) AS h_score
               FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               LEFT JOIN (
                   SELECT DISTINCT ON (account_id) account_id, health_score
                   FROM account_health_history ORDER BY account_id, checked_at DESC
               ) ah ON ah.account_id=a.id
               WHERE m.ecosystem_id=$1 AND m.object_type='account' AND a.is_active=TRUE""",
            ecosystem_id,
        )
        h.account_count = len(acc_rows)
        if acc_rows:
            import datetime as _dt
            healthy = 0
            for r in acc_rows:
                cd = r["cooldown_until"]
                in_cd = cd and (cd.replace(tzinfo=None) if hasattr(cd, 'tzinfo') and cd.tzinfo else cd) > _dt.datetime.utcnow()
                restricted = (r["flood_count_7d"] or 0) > 3
                if not in_cd and not restricted:
                    healthy += 1
            h.healthy_accounts = healthy
            avg_trust = sum(r["trust_score"] or 0.5 for r in acc_rows) / len(acc_rows)
            avg_health = sum(r["h_score"] or 0.7 for r in acc_rows) / len(acc_rows)
            ready_ratio = healthy / len(acc_rows)
            h.health_score = round(avg_trust * 0.4 + avg_health * 0.35 + ready_ratio * 0.25, 3)
            h.restrictions_count = len(acc_rows) - healthy

        # Proxies: ratio active/total
        proxy_rows = await pool.fetch(
            """SELECT p.is_active, COALESCE(p.last_check_ok, TRUE) AS ok
               FROM ecosystem_members m
               JOIN user_proxies p ON p.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='proxy'""",
            ecosystem_id,
        )
        h.active_proxies = len(proxy_rows)
        if proxy_rows:
            h.healthy_proxies = sum(1 for r in proxy_rows if r["is_active"] and r["ok"])
            proxy_ratio = h.healthy_proxies / len(proxy_rows)
        else:
            proxy_ratio = 1.0  # no proxies = not counted as risk

        # Operations: recent success rate (7 days)
        op_row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='done') AS done,
                   COUNT(*) AS total
               FROM operation_queue
               WHERE owner_id=$2
                 AND created_at > NOW() - INTERVAL '7 days'
                 AND status IN ('done', 'failed')""",
            ecosystem_id, owner_id,
        )
        if op_row and (op_row["total"] or 0) > 0:
            h.recent_op_success_rate = round(
                (op_row["done"] or 0) / op_row["total"], 3
            )

        # Stability: based on variance of success rate and account churn
        h.stability_score = round(
            h.recent_op_success_rate * 0.5 + proxy_ratio * 0.3 + h.health_score * 0.2, 3
        )

        # Reliability: infra_memory for member accounts
        mem_rows = await pool.fetch(
            """SELECT success_rate FROM infra_memory_accounts im
               JOIN ecosystem_members m ON m.object_id=im.account_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND im.total_ops >= 5""",
            ecosystem_id,
        )
        if mem_rows:
            avg_mem_rate = sum(r["success_rate"] or 0 for r in mem_rows) / len(mem_rows)
            h.reliability_score = round(avg_mem_rate, 3)
        else:
            h.reliability_score = h.health_score

        # Recovery: accounts with low cooldown history
        long_cd = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.cooldown_until > NOW() + INTERVAL '12 hours'""",
            ecosystem_id,
        ) or 0
        if h.account_count > 0:
            h.recovery_score = round(1.0 - min(long_cd / h.account_count, 1.0), 3)

        # Growth: new members in last 7 days / total
        new_members = await pool.fetchval(
            "SELECT COUNT(*) FROM ecosystem_members WHERE ecosystem_id=$1 AND added_at > NOW()-INTERVAL '7 days'",
            ecosystem_id,
        ) or 0
        total_members = await pool.fetchval(
            "SELECT COUNT(*) FROM ecosystem_members WHERE ecosystem_id=$1",
            ecosystem_id,
        ) or 1
        h.growth_score = round(min(new_members / max(total_members, 1), 1.0), 3)

    except Exception as e:
        log.debug("compute_health eco=%d: %s", ecosystem_id, e)

    return h


# ── Pressure computation ──────────────────────────────────────────────────────

async def compute_pressure(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> EcosystemPressure:
    p = EcosystemPressure()
    try:
        # Active operations for this owner
        active_ops = await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='running'",
            owner_id,
        ) or 0
        p.active_tasks = active_ops

        # Cooldown ratio in ecosystem accounts
        acc_total = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account' AND a.is_active=TRUE""",
            ecosystem_id,
        ) or 0
        acc_cd = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.cooldown_until > NOW()""",
            ecosystem_id,
        ) or 0
        if acc_total > 0:
            p.cooldown_ratio = round(acc_cd / acc_total, 3)

        # Overloaded accounts (flood_count_7d > 5)
        p.overloaded_accounts = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND (a.flood_count_7d or 0) > 5""",
            ecosystem_id,
        ) or 0

        # Overloaded proxies (need proxy_quality_log if available)
        p.overloaded_proxies = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN user_proxies pr ON pr.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='proxy'
                 AND pr.is_active=FALSE""",
            ecosystem_id,
        ) or 0

        # Operation density: ops last 24h / member account count
        ops_24h = await pool.fetchval(
            """SELECT COUNT(*) FROM operation_queue
               WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '24 hours'""",
            owner_id,
        ) or 0
        p.operation_density = round(ops_24h / max(acc_total, 1), 2)

        # Score computation
        cooldown_component = p.cooldown_ratio * 40
        overload_component = min(p.overloaded_accounts / max(acc_total, 1), 1.0) * 25
        task_component = min(active_ops / 5, 1.0) * 20
        density_component = min(p.operation_density / 3, 1.0) * 15

        p.score = int(min(cooldown_component + overload_component + task_component + density_component, 100))
        p.account_load = round(cooldown_component / 40, 3) if acc_total else 0.0

    except Exception as e:
        log.debug("compute_pressure eco=%d: %s", ecosystem_id, e)

    return p


# ── Risk computation ──────────────────────────────────────────────────────────

async def compute_risk(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> EcosystemRisk:
    r = EcosystemRisk()
    try:
        # Account risk: low trust + restrictions
        acc_rows = await pool.fetch(
            """SELECT a.trust_score, a.flood_count_7d,
                      a.cooldown_until > NOW() AS in_cooldown
               FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account' AND a.is_active""",
            ecosystem_id,
        )
        if acc_rows:
            low_trust = sum(1 for a in acc_rows if (a["trust_score"] or 0.5) < 0.4)
            in_cd = sum(1 for a in acc_rows if a["in_cooldown"])
            r.account_risk = round(
                (low_trust / len(acc_rows)) * 0.6
                + (in_cd / len(acc_rows)) * 0.4,
                3,
            )
            if low_trust > 0:
                r.reasons.append(f"Аккаунтов с низким trust: {low_trust}")
            if in_cd > 0:
                r.reasons.append(f"На кулдауне: {in_cd}/{len(acc_rows)} аккаунтов")

        # Operational risk: recent failure rate
        op_row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='done') AS done,
                   COUNT(*) FILTER (WHERE status='failed') AS failed
               FROM operation_queue
               WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '7 days'
                 AND status IN ('done', 'failed')""",
            owner_id,
        )
        if op_row:
            total = (op_row["done"] or 0) + (op_row["failed"] or 0)
            if total > 0:
                fail_rate = (op_row["failed"] or 0) / total
                r.operational_risk = round(fail_rate, 3)
                if fail_rate > 0.3:
                    r.reasons.append(f"Высокий процент ошибок операций: {fail_rate:.0%}")

        # Infrastructure risk: pressure
        p = await compute_pressure(pool, ecosystem_id, owner_id)
        r.infrastructure_risk = round(p.score / 100, 3)
        if p.score >= 70:
            r.reasons.append(f"Высокое давление инфраструктуры: {p.score}")

        # Proxy risk
        proxy_total = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN user_proxies pr ON pr.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='proxy'""",
            ecosystem_id,
        ) or 0
        proxy_bad = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN user_proxies pr ON pr.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='proxy' AND pr.is_active=FALSE""",
            ecosystem_id,
        ) or 0
        if proxy_total > 0:
            r.proxy_risk = round(proxy_bad / proxy_total, 3)
            if r.proxy_risk > 0.5:
                r.reasons.append(f"Проблемные прокси: {proxy_bad}/{proxy_total}")

        # Recovery risk: long cooldowns
        long_cd = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.cooldown_until > NOW()+INTERVAL '24 hours'""",
            ecosystem_id,
        ) or 0
        total_accs = len(acc_rows) if acc_rows else 1
        r.recovery_risk = round(min(long_cd / total_accs, 1.0), 3)
        if long_cd > 0:
            r.reasons.append(f"Длительный кулдаун (>24ч): {long_cd} аккаунтов")

    except Exception as e:
        log.debug("compute_risk eco=%d: %s", ecosystem_id, e)

    return r


# ── Drift detection ───────────────────────────────────────────────────────────

async def detect_drift(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> list[EcosystemDrift]:
    """Обнаруживает отклонения внутри экосистемы."""
    drifts: list[EcosystemDrift] = []
    try:
        # 1. Аккаунты без прокси в экосистемах где у других аккаунтов прокси есть
        acc_with_proxy = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.proxy_id IS NOT NULL""",
            ecosystem_id,
        ) or 0
        acc_without_proxy = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.proxy_id IS NULL AND a.is_active""",
            ecosystem_id,
        ) or 0
        if acc_with_proxy > 0 and acc_without_proxy > 0:
            drifts.append(EcosystemDrift(
                drift_type="resource_gap",
                object_type="account",
                object_id=None,
                description=f"{acc_without_proxy} аккаунтов без прокси, хотя в экосистеме использование прокси активно",
                suggested_fix="Назначить прокси всем аккаунтам экосистемы",
                auto_fixable=False,
            ))

        # 2. Аккаунты с trust < 0.3 в активной экосистеме
        low_trust_accs = await pool.fetch(
            """SELECT a.id, COALESCE(a.first_name, a.phone, 'id'||a.id::text) AS label,
                      a.trust_score
               FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.is_active AND COALESCE(a.trust_score, 0.5) < 0.3""",
            ecosystem_id,
        )
        for a in low_trust_accs[:3]:
            drifts.append(EcosystemDrift(
                drift_type="config_deviation",
                object_type="account",
                object_id=a["id"],
                description=f"Аккаунт {a['label']} trust={a['trust_score']:.2f} — крайне низкий",
                suggested_fix="Заменить аккаунт или назначить прогрев",
                auto_fixable=False,
            ))

        # 3. Экосистема без аккаунтов
        acc_count = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members
               WHERE ecosystem_id=$1 AND object_type='account'""",
            ecosystem_id,
        ) or 0
        if acc_count == 0:
            drifts.append(EcosystemDrift(
                drift_type="resource_gap",
                object_type="account",
                object_id=None,
                description="Экосистема не содержит ни одного аккаунта",
                suggested_fix="Добавить хотя бы один активный аккаунт",
                auto_fixable=False,
            ))

        # 4. Заблокированные аккаунты в экосистеме
        banned_accs = await pool.fetch(
            """SELECT a.id, COALESCE(a.first_name, a.phone, 'id'||a.id::text) AS label
               FROM ecosystem_members m
               JOIN tg_accounts a ON a.id=m.object_id
               WHERE m.ecosystem_id=$1 AND m.object_type='account'
                 AND a.is_banned = TRUE""",
            ecosystem_id,
        )
        for ba in banned_accs[:3]:
            drifts.append(EcosystemDrift(
                drift_type="account_banned",
                object_type="account",
                object_id=ba["id"],
                description=f"Аккаунт {ba['label']} заблокирован (banned)",
                suggested_fix="Удалить аккаунт из экосистемы или заменить его",
                auto_fixable=False,
            ))

        # 5. Экосистема без операций 7+ дней (застой)
        last_event = await pool.fetchval(
            """SELECT MAX(occurred_at) FROM ecosystem_events
               WHERE ecosystem_id=$1""",
            ecosystem_id,
        )
        from datetime import datetime as _dt_now, timezone as _tz
        if last_event is None or (
            _dt_now.now(tz=_tz.utc) - last_event.astimezone(_tz.utc)
        ).days >= 7:
            drifts.append(EcosystemDrift(
                drift_type="inactivity",
                object_type=None,
                object_id=None,
                description="Нет активности в экосистеме более 7 дней",
                suggested_fix="Запустите операцию или обновите статус экосистемы",
                auto_fixable=False,
            ))

        # 6. Высокий cooldown ratio (>= 60% аккаунтов на cooldown)
        if acc_count > 0:
            cooling_count = await pool.fetchval(
                """SELECT COUNT(*) FROM ecosystem_members m
                   JOIN tg_accounts a ON a.id=m.object_id
                   WHERE m.ecosystem_id=$1 AND m.object_type='account'
                     AND a.is_active AND a.cooldown_until > NOW()""",
                ecosystem_id,
            ) or 0
            cooldown_ratio = cooling_count / acc_count
            if cooldown_ratio >= 0.6:
                drifts.append(EcosystemDrift(
                    drift_type="resource_pressure",
                    object_type="account",
                    object_id=None,
                    description=f"{cooling_count}/{acc_count} аккаунтов на cooldown ({cooldown_ratio:.0%})",
                    suggested_fix="Снизьте интенсивность операций или добавьте новые аккаунты",
                    auto_fixable=False,
                ))

        # 7. Экосистема без каналов/групп и без ботов (пустая структура)
        channel_count = await pool.fetchval(
            """SELECT COUNT(*) FROM ecosystem_members
               WHERE ecosystem_id=$1 AND object_type IN ('channel', 'group', 'bot')""",
            ecosystem_id,
        ) or 0
        if acc_count > 0 and channel_count == 0:
            drifts.append(EcosystemDrift(
                drift_type="resource_gap",
                object_type=None,
                object_id=None,
                description="В экосистеме нет каналов, групп или ботов",
                suggested_fix="Добавьте каналы/группы/боты или запустите Global Presence",
                auto_fixable=False,
            ))

        # Save all detected drifts to ecosystem_drift_log
        for d in drifts:
            try:
                await pool.execute(
                    """INSERT INTO ecosystem_drift_log
                       (ecosystem_id, owner_id, drift_type, object_type, object_id, description, suggested_fix, auto_fixable)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                       ON CONFLICT DO NOTHING""",
                    ecosystem_id, owner_id, d.drift_type, d.object_type, d.object_id,
                    d.description, d.suggested_fix, d.auto_fixable,
                )
            except Exception:
                pass

    except Exception as e:
        log.debug("detect_drift eco=%d: %s", ecosystem_id, e)

    return drifts


# ── Full snapshot ─────────────────────────────────────────────────────────────

async def get_snapshot(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> Optional[EcosystemSnapshot]:
    eco = await get_ecosystem(pool, ecosystem_id, owner_id)
    if not eco:
        return None

    health, pressure, risk, drifts = await asyncio.gather(
        compute_health(pool, ecosystem_id, owner_id),
        compute_pressure(pool, ecosystem_id, owner_id),
        compute_risk(pool, ecosystem_id, owner_id),
        detect_drift(pool, ecosystem_id, owner_id),
        return_exceptions=True,
    )

    if isinstance(health, Exception):
        health = EcosystemHealth()
    if isinstance(pressure, Exception):
        pressure = EcosystemPressure()
    if isinstance(risk, Exception):
        risk = EcosystemRisk()
    if isinstance(drifts, Exception):
        drifts = []

    # Member counts by type
    counts_rows = await pool.fetch(
        """SELECT object_type, COUNT(*) AS cnt
           FROM ecosystem_members WHERE ecosystem_id=$1 GROUP BY object_type""",
        ecosystem_id,
    )
    member_counts = {r["object_type"]: r["cnt"] for r in counts_rows}

    # Recent events count
    recent_events = await pool.fetchval(
        "SELECT COUNT(*) FROM ecosystem_events WHERE ecosystem_id=$1 AND occurred_at > NOW()-INTERVAL '7 days'",
        ecosystem_id,
    ) or 0

    # Cache scores in DB
    try:
        await pool.execute(
            """UPDATE ecosystems SET
               health_score=$3, stability_score=$4, reliability_score=$5,
               recovery_score=$6, growth_score=$7, pressure_score=$8,
               risk_level=$9, updated_at=NOW()
               WHERE id=$1 AND owner_id=$2""",
            ecosystem_id, owner_id,
            health.health_score, health.stability_score, health.reliability_score,
            health.recovery_score, health.growth_score, pressure.score,
            risk.level,
        )
    except Exception:
        pass

    return EcosystemSnapshot(
        ecosystem_id=ecosystem_id,
        name=eco["name"],
        ecosystem_type=eco["ecosystem_type"],
        status=eco["status"],
        health=health,
        pressure=pressure,
        risk=risk,
        drifts=drifts,
        member_counts=member_counts,
        recent_events=recent_events,
    )


# ── Auto-discover members ─────────────────────────────────────────────────────

async def auto_discover_members(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int
) -> dict[str, int]:
    """Автоматически привязывает объекты к экосистеме через кластер или пулы.

    Возвращает словарь {object_type: добавлено_штук}.
    """
    eco = await get_ecosystem(pool, ecosystem_id, owner_id)
    if not eco:
        return {}

    added: dict[str, int] = {}

    try:
        # Accounts: по region если задан, иначе все активные без экосистемы
        if eco["region"]:
            acc_rows = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE
                     AND NOT EXISTS (
                         SELECT 1 FROM ecosystem_members em
                         WHERE em.object_type='account' AND em.object_id=tg_accounts.id
                     )
                   LIMIT 20""",
                owner_id,
            )
        else:
            acc_rows = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE
                     AND NOT EXISTS (
                         SELECT 1 FROM ecosystem_members em
                         WHERE em.object_type='account' AND em.object_id=tg_accounts.id
                     )
                   ORDER BY COALESCE(trust_score, 0.5) DESC
                   LIMIT 10""",
                owner_id,
            )
        n = 0
        for r in acc_rows:
            ok = await add_member(pool, ecosystem_id, owner_id, "account", r["id"])
            if ok:
                n += 1
        if n:
            added["account"] = n

        # Channels
        ch_rows = await pool.fetch(
            "SELECT DISTINCT channel_id AS id FROM managed_channels WHERE owner_id=$1 LIMIT 20",
            owner_id,
        )
        n = 0
        for r in ch_rows:
            ok = await add_member(pool, ecosystem_id, owner_id, "channel", r["id"])
            if ok:
                n += 1
        if n:
            added["channel"] = n

        # Bots
        bot_rows = await pool.fetch(
            "SELECT bot_id AS id FROM managed_bots WHERE added_by=$1 AND is_active LIMIT 10",
            owner_id,
        )
        n = 0
        for r in bot_rows:
            ok = await add_member(pool, ecosystem_id, owner_id, "bot", r["id"])
            if ok:
                n += 1
        if n:
            added["bot"] = n

    except Exception as e:
        log.debug("auto_discover eco=%d: %s", ecosystem_id, e)

    return added


# ── Format helpers ────────────────────────────────────────────────────────────

def format_snapshot(snap: EcosystemSnapshot) -> str:
    """Форматирует полный снимок экосистемы для Telegram HTML."""
    type_labels = {
        "custom": "Пользовательская",
        "regional": "Региональная",
        "global_presence": "Глобальное присутствие",
        "media_network": "Медиасеть",
        "strike_network": "Strike-сеть",
    }
    eco_type = type_labels.get(snap.ecosystem_type, snap.ecosystem_type)

    members_str = " | ".join(
        f"{_type_icon(t)} {c}" for t, c in snap.member_counts.items() if c > 0
    ) or "—"

    lines = [
        f"🌐 <b>{html.escape(snap.name)}</b>",
        f"<i>{eco_type}</i>  •  {members_str}",
        "",
        "📊 <b>Здоровье</b>",
        f"  {snap.health.grade} ({snap.health.overall:.0%})",
        f"  Аккаунтов: {snap.health.healthy_accounts}/{snap.health.account_count} готовы",
        f"  Успешность операций: {snap.health.recent_op_success_rate:.0%}",
        "",
        f"⚡ <b>Давление</b>  {snap.pressure.emoji} {snap.pressure.score}/100",
        f"  {snap.pressure.level}",
    ]
    if snap.pressure.active_tasks:
        lines.append(f"  Активных задач: {snap.pressure.active_tasks}")
    if snap.pressure.overloaded_accounts:
        lines.append(f"  Перегруженных аккаунтов: {snap.pressure.overloaded_accounts}")

    lines += [
        "",
        f"⚠️ <b>Риск</b>  {snap.risk.level_label}",
    ]
    for reason in snap.risk.reasons[:3]:
        lines.append(f"  • {html.escape(reason)}")

    if snap.drifts:
        lines.append(f"\n🔀 <b>Дрейф</b>  {len(snap.drifts)} проблем")
        for d in snap.drifts[:2]:
            lines.append(f"  • {html.escape(d.description[:80])}")

    if snap.recent_events:
        lines.append(f"\n📋 Событий за 7 дней: {snap.recent_events}")

    return "\n".join(lines)


def _type_icon(object_type: str) -> str:
    return {"account": "📱", "channel": "📡", "group": "👥", "bot": "🤖", "proxy": "🌐"}.get(object_type, "•")


def format_health_bar(score: float, width: int = 8) -> str:
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def format_risk_reasons(risk: EcosystemRisk) -> str:
    if not risk.reasons:
        return "Критических проблем нет"
    return "\n".join(f"• {html.escape(r)}" for r in risk.reasons[:5])


# ── DNA Templates ─────────────────────────────────────────────────────────────

async def create_dna(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    dna_type: str,
    description: str = "",
    template_data: Optional[dict] = None,
    is_public: bool = False,
) -> int:
    """Создаёт DNA-шаблон. Возвращает id."""
    import json as _json
    row = await pool.fetchrow(
        """INSERT INTO ecosystem_dna (owner_id, name, dna_type, description, template_data, is_public)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id""",
        owner_id, name, dna_type, description,
        _json.dumps(template_data or {}), is_public,
    )
    return row["id"]


async def list_dna(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Список DNA-шаблонов владельца (+ публичные)."""
    rows = await pool.fetch(
        """SELECT * FROM ecosystem_dna
           WHERE owner_id=$1 OR is_public=TRUE
           ORDER BY created_at DESC""",
        owner_id,
    )
    return [dict(r) for r in rows]


async def get_dna(pool: asyncpg.Pool, dna_id: int, owner_id: int) -> Optional[dict]:
    row = await pool.fetchrow(
        "SELECT * FROM ecosystem_dna WHERE id=$1 AND (owner_id=$2 OR is_public=TRUE)",
        dna_id, owner_id,
    )
    return dict(row) if row else None


async def delete_dna(pool: asyncpg.Pool, dna_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM ecosystem_dna WHERE id=$1 AND owner_id=$2",
        dna_id, owner_id,
    )


async def capture_dna_from_ecosystem(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int, name: str
) -> int:
    """Снимает DNA-слепок с текущего состояния экосистемы. Возвращает dna_id."""
    eco = await get_ecosystem(pool, ecosystem_id, owner_id)
    if not eco:
        raise ValueError(f"Ecosystem {ecosystem_id} not found")

    # Member counts by type
    counts_rows = await pool.fetch(
        """SELECT object_type, COUNT(*) AS cnt
           FROM ecosystem_members WHERE ecosystem_id=$1 GROUP BY object_type""",
        ecosystem_id,
    )
    member_counts = {r["object_type"]: r["cnt"] for r in counts_rows}

    import json as _json
    meta = eco.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}

    template_data = {
        "ecosystem_type": eco["ecosystem_type"],
        "description": eco.get("description", ""),
        "region": eco.get("region"),
        "member_counts": member_counts,
        "meta": meta,
        "source_ecosystem_id": ecosystem_id,
    }
    return await create_dna(
        pool, owner_id, name, eco["ecosystem_type"],
        description=f"Снято с экосистемы: {eco['name']}",
        template_data=template_data,
    )


async def apply_dna_to_ecosystem(
    pool: asyncpg.Pool, dna_id: int, ecosystem_id: int, owner_id: int,
) -> dict:
    """Применяет DNA-шаблон к экосистеме (тип, регион, описание).
    Возвращает словарь с тем что было изменено."""
    dna = await get_dna(pool, dna_id, owner_id)
    if not dna:
        raise ValueError(f"DNA {dna_id} not found")

    import json as _json
    td = dna.get("template_data") or {}
    if isinstance(td, str):
        try:
            td = _json.loads(td)
        except Exception:
            td = {}

    changes: dict[str, str] = {}

    eco_type = td.get("ecosystem_type")
    region   = td.get("region")

    if eco_type:
        await pool.execute(
            "UPDATE ecosystems SET ecosystem_type=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
            eco_type, ecosystem_id, owner_id,
        )
        changes["ecosystem_type"] = eco_type

    if region:
        await pool.execute(
            "UPDATE ecosystems SET region=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
            region, ecosystem_id, owner_id,
        )
        changes["region"] = region

    # Link dna_id to ecosystem
    await pool.execute(
        "UPDATE ecosystems SET dna_id=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
        dna_id, ecosystem_id, owner_id,
    )
    changes["dna_id"] = str(dna_id)

    await record_event(
        pool, ecosystem_id, owner_id,
        "dna_applied", f"Применена DNA: {dna['name']}",
        severity="info", details={"dna_id": dna_id, "changes": changes},
    )
    return changes


# ── Clone ─────────────────────────────────────────────────────────────────────

async def clone_ecosystem(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int, new_name: str
) -> int:
    """Клонирует экосистему: создаёт новую с теми же настройками и составом.
    Возвращает id новой экосистемы."""
    eco = await get_ecosystem(pool, ecosystem_id, owner_id)
    if not eco:
        raise ValueError(f"Ecosystem {ecosystem_id} not found")

    new_id = await create_ecosystem(
        pool, owner_id, new_name,
        description=f"Клон: {eco.get('description', '')}",
        ecosystem_type=eco["ecosystem_type"],
        region=eco.get("region"),
    )

    # Copy all members
    members = await get_members(pool, ecosystem_id, owner_id)
    for m in members:
        try:
            await add_member(pool, new_id, owner_id, m["object_type"], m["object_id"], m.get("role", "member"))
        except Exception:
            pass

    await record_event(
        pool, new_id, owner_id,
        "cloned", f"Клонирована из экосистемы #{ecosystem_id}: {eco['name']}",
        severity="info", details={"source_ecosystem_id": ecosystem_id},
    )
    return new_id


# ── Sync scores ───────────────────────────────────────────────────────────────

async def sync_ecosystem_scores(
    pool: asyncpg.Pool, ecosystem_id: int, owner_id: int,
) -> dict:
    """Пересчитывает и сохраняет health/pressure/risk/growth в строке экосистемы.
    Возвращает обновлённые значения."""
    health, pressure, risk = await asyncio.gather(
        compute_health(pool, ecosystem_id, owner_id),
        compute_pressure(pool, ecosystem_id, owner_id),
        compute_risk(pool, ecosystem_id, owner_id),
        return_exceptions=True,
    )

    if isinstance(health, Exception):
        health = EcosystemHealth()
    if isinstance(pressure, Exception):
        pressure = EcosystemPressure()
    if isinstance(risk, Exception):
        risk = EcosystemRisk()

    await pool.execute(
        """UPDATE ecosystems SET
               health_score      = $1,
               stability_score   = $2,
               reliability_score = $3,
               recovery_score    = $4,
               growth_score      = $5,
               pressure_score    = $6,
               risk_level        = $7,
               updated_at        = now()
           WHERE id=$8 AND owner_id=$9""",
        health.health_score,
        health.stability_score,
        health.reliability_score,
        health.recovery_score,
        health.growth_score,
        pressure.score,
        risk.level,
        ecosystem_id, owner_id,
    )

    await record_event(
        pool, ecosystem_id, owner_id,
        "scores_synced", "Метрики экосистемы синхронизированы",
        severity="info",
        details={
            "health": round(health.overall, 3),
            "pressure": pressure.score,
            "risk": risk.level,
        },
    )

    return {
        "health": round(health.overall, 3),
        "pressure": pressure.score,
        "risk_level": risk.level,
        "stability": round(health.stability_score, 3),
        "reliability": round(health.reliability_score, 3),
    }
