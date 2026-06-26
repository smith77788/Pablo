"""
Intelligence Engine — единый аналитический слой BotMother. EPOCH II.

Принцип: данные без решений — не интеллект.
Система превращает данные в конкретные решения:

  analyze_accounts()          → Account Suitability, Risk, Reliability Score per account
  analyze_proxies()           → Proxy Quality + Risk per proxy
  assess_risk()               → LOW/MEDIUM/HIGH/CRITICAL + конкретные причины
  predict_operation()         → время, вероятность успеха, ожидаемые ошибки
  get_pre_launch_intelligence() → всё вместе + рекомендуемые аккаунты + решение

Использует: infra_memory, infra_pressure, infra_advisor, flood_engine, DB.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

from services.account_manager import effective_account_status
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# ── Константы времён операций (секунд на 1 элемент, нижняя/верхняя) ──────────
_OP_TIMINGS: dict[str, tuple[float, float]] = {
    "bulk_join": (15.0, 35.0),
    "bulk_leave": (8.0, 20.0),
    "mass_publish": (5.0, 15.0),
    "strike": (20.0, 60.0),
    "dm_campaign": (10.0, 28.0),
    "global_presence": (30.0, 90.0),
    "invite_users": (12.0, 30.0),
    "bulk_create_channels": (60.0, 180.0),
    "parse": (2.0, 8.0),
    "default": (10.0, 30.0),
}

# Успешность операции по историческим данным (базовые prior-значения)
_BASE_SUCCESS_RATES: dict[str, float] = {
    "bulk_join": 0.82,
    "bulk_leave": 0.91,
    "mass_publish": 0.88,
    "strike": 0.75,
    "dm_campaign": 0.78,
    "global_presence": 0.70,
    "invite_users": 0.80,
    "bulk_create_channels": 0.72,
    "parse": 0.90,
    "default": 0.80,
}

# Максимальное число элементов на аккаунт за один прогон
_MAX_ITEMS_PER_ACCOUNT: dict[str, int] = {
    "bulk_join": 50,
    "bulk_leave": 100,
    "mass_publish": 200,
    "strike": 30,
    "dm_campaign": 80,
    "global_presence": 40,
    "invite_users": 60,
    "bulk_create_channels": 10,
    "parse": 500,
    "default": 100,
}


# ── Датаклассы ────────────────────────────────────────────────────────────────


@dataclass
class AccountIntelligence:
    """Оценка одного аккаунта для операции."""

    account_id: int
    phone: str = ""
    first_name: str = ""

    # Composite scores (0.0–1.0)
    suitability_score: float = 0.5  # насколько аккаунт подходит для op_type
    risk_score: float = (
        0.5  # насколько рискованно использование (0=безопасно, 1=опасно)
    )
    reliability_score: float = 0.5  # историческая надёжность из infra_memory

    # Raw data
    trust_score: float = 1.0
    flood_count_7d: int = 0
    is_cooling: bool = False
    cooldown_minutes: int = 0
    pool: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    memory_successes: int = 0
    memory_failures: int = 0

    # Решение
    recommended: bool = False
    skip_reason: str = ""  # почему не рекомендуется (если recommended=False)

    def label(self) -> str:
        return self.first_name or self.phone or f"acc:{self.account_id}"


@dataclass
class ProxyIntelligence:
    """Оценка одного прокси."""

    proxy_id: int
    label: str = ""
    proxy_type: str = "socks5"

    quality_score: float = 0.5  # 0=плохой, 1=отличный
    risk_score: float = 0.5  # 0=безопасный, 1=рискованный
    success_rate: float = 0.5
    avg_latency_ms: float = 0.0
    total_checks: int = 0
    recent_failures: int = 0

    recommended: bool = False


@dataclass
class RiskAssessment:
    """Оценка риска операции."""

    level: str = "low"  # "low" | "medium" | "high" | "critical"
    level_emoji: str = "🟢"
    score: int = 0  # 0–100
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # причины почему запустить нельзя
    recommendations: list[str] = field(default_factory=list)
    safe_to_proceed: bool = True

    @property
    def summary(self) -> str:
        return f"{self.level_emoji} {self.level.upper()} ({self.score}/100)"


@dataclass
class OperationPrediction:
    """Прогноз выполнения операции."""

    op_type: str
    item_count: int
    account_count: int

    estimated_minutes: int = 0
    estimated_minutes_min: int = 0
    estimated_minutes_max: int = 0
    success_probability: float = 0.8  # 0.0–1.0
    expected_success_items: int = 0
    expected_failed_items: int = 0
    items_per_account: float = 0.0

    confidence: str = "low"  # "low" | "medium" | "high" — насколько точен прогноз

    def format(self) -> str:
        pct = int(self.success_probability * 100)
        if self.estimated_minutes_min == self.estimated_minutes_max:
            time_str = f"{self.estimated_minutes} мин"
        else:
            time_str = f"{self.estimated_minutes_min}–{self.estimated_minutes_max} мин"
        return (
            f"⏱ {time_str} · ✅ {pct}% успеха · "
            f"✔ {self.expected_success_items}/{self.item_count} элементов"
        )


@dataclass
class PreLaunchIntelligence:
    """Комплексная оценка перед запуском операции."""

    op_type: str
    item_count: int
    owner_id: int

    risk: RiskAssessment = field(default_factory=RiskAssessment)
    prediction: OperationPrediction = field(
        default_factory=lambda: OperationPrediction("default", 0, 0)
    )
    recommended_accounts: list[AccountIntelligence] = field(default_factory=list)
    all_accounts: list[AccountIntelligence] = field(default_factory=list)
    pressure_score: int = 0
    pressure_label: str = "Норма"
    pressure_emoji: str = "🟢"

    # Прокси
    recommended_proxies: list["ProxyIntelligence"] = field(default_factory=list)
    all_proxies: list["ProxyIntelligence"] = field(default_factory=list)

    # Итоговое решение системы
    go_decision: bool = True  # True = запускать, False = блокировать
    go_reason: str = ""  # почему заблокировано
    warning_text: str = ""  # предупреждение (если go=True но есть риски)

    def format_header(self) -> str:
        icon = "🚀" if self.go_decision else "🚫"
        return f"{icon} <b>Pre-Launch Intelligence</b> · {self.op_type}"


# ── Главные точки входа ───────────────────────────────────────────────────────


async def analyze_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str,
    account_ids: Optional[list[int]] = None,
) -> list[AccountIntelligence]:
    """Оценить аккаунты для указанного типа операции.

    Возвращает список AccountIntelligence, отсортированный по suitability_score убывающий.
    Вычисляет: Suitability Score, Risk Score, Reliability Score.
    """
    try:
        return await _analyze_accounts_impl(pool, owner_id, action_type, account_ids)
    except Exception as e:
        log_exc_swallow(
            log,
            "intelligence_engine.analyze_accounts owner=%d op=%s: %s",
            owner_id,
            action_type,
            e,
        )
        return []


async def analyze_proxies(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[ProxyIntelligence]:
    """Оценить прокси по Quality Score + Risk Score."""
    try:
        return await _analyze_proxies_impl(pool, owner_id)
    except Exception as e:
        log_exc_swallow(
            log, "intelligence_engine.analyze_proxies owner=%d: %s", owner_id, e
        )
        return []


async def assess_risk(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
) -> RiskAssessment:
    """Оценить риск операции. Возвращает LOW/MEDIUM/HIGH/CRITICAL с причинами."""
    try:
        return await _assess_risk_impl(pool, owner_id, op_type, item_count)
    except Exception as e:
        log_exc_swallow(
            log,
            "intelligence_engine.assess_risk owner=%d op=%s: %s",
            owner_id,
            op_type,
            e,
        )
        return RiskAssessment(
            level="medium", level_emoji="🟡", score=50, safe_to_proceed=True
        )


async def predict_operation(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
    account_count: Optional[int] = None,
) -> OperationPrediction:
    """Прогноз выполнения операции: время, вероятность успеха, ожидаемые ошибки."""
    try:
        return await _predict_impl(pool, owner_id, op_type, item_count, account_count)
    except Exception as e:
        log_exc_swallow(
            log,
            "intelligence_engine.predict_operation owner=%d op=%s: %s",
            owner_id,
            op_type,
            e,
        )
        return _fallback_prediction(op_type, item_count, account_count or 1)


async def get_pre_launch_intelligence(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
    account_ids: Optional[list[int]] = None,
) -> PreLaunchIntelligence:
    """Комплексная pre-launch оценка: риск + прогноз + рекомендуемые аккаунты + решение.

    Единая точка входа для STRIKE, Mass Ops, Global Presence, DM-кампаний.
    """
    try:
        return await _pre_launch_impl(pool, owner_id, op_type, item_count, account_ids)
    except Exception as e:
        log_exc_swallow(
            log,
            "intelligence_engine.get_pre_launch_intelligence owner=%d op=%s: %s",
            owner_id,
            op_type,
            e,
        )
        result = PreLaunchIntelligence(
            op_type=op_type, item_count=item_count, owner_id=owner_id
        )
        result.go_decision = True
        result.warning_text = "⚠️ Оценка недоступна — продолжите вручную"
        return result


# ── Форматирование для Telegram ───────────────────────────────────────────────


def format_pre_launch_block(intel: PreLaunchIntelligence) -> str:
    """Форматировать блок pre-launch intelligence для вставки в confirm-экран."""
    lines: list[str] = []

    # Давление
    lines.append(
        f"{intel.pressure_emoji} <b>Инфраструктура:</b> {intel.pressure_label} ({intel.pressure_score}/100)"
    )

    # Аккаунты — доступные, кулдаун и прочие исключённые (без двойного счёта)
    available = [a for a in intel.all_accounts if a.recommended]
    cooling = [a for a in intel.all_accounts if a.is_cooling]
    excluded_other = [
        a for a in intel.all_accounts if not a.recommended and not a.is_cooling
    ]
    accs_line = f"📱 <b>Аккаунты:</b> ✅ {len(available)} доступно"
    if cooling:
        accs_line += f" · ⏳ {len(cooling)} кулдаун"
    if excluded_other:
        accs_line += f" · 🚫 {len(excluded_other)} исключено"
    lines.append(accs_line)

    # Исключённые (не на кулдауне) с причинами — макс. 3
    if excluded_other:
        for acc in excluded_other[:3]:
            lbl = html.escape(acc.label())
            reason = html.escape(acc.skip_reason)
            lines.append(f"   ↳ {lbl}: {reason}")
        if len(excluded_other) > 3:
            lines.append(f"   … и ещё {len(excluded_other) - 3}")

    # Прокси (только если есть)
    if intel.all_proxies:
        bad_proxies = [p for p in intel.all_proxies if not p.recommended]
        proxy_line = f"🌐 <b>Прокси:</b> ✅ {len(intel.recommended_proxies)} пригодны"
        if bad_proxies:
            proxy_line += f" · ⚠️ {len(bad_proxies)} плохих"
        lines.append(proxy_line)

    # Прогноз
    if intel.prediction.item_count > 0:
        lines.append(f"⏱ <b>Прогноз:</b> {intel.prediction.format()}")

    # Риск
    lines.append(f"🎯 <b>Риск:</b> {html.escape(intel.risk.summary)}")

    # Причины риска (макс. 2)
    if intel.risk.reasons:
        for r in intel.risk.reasons[:2]:
            lines.append(f"   • {html.escape(r)}")

    # Предупреждение (если есть)
    if intel.warning_text:
        lines.append(f"\n⚠️ {html.escape(intel.warning_text)}")

    # Блокировка
    if not intel.go_decision:
        lines.append(
            f"\n🚫 <b>Операция заблокирована:</b> {html.escape(intel.go_reason)}"
        )

    return "\n".join(lines)


def format_account_intelligence_list(
    accounts: list[AccountIntelligence],
    max_items: int = 5,
) -> str:
    """Форматировать список оценок аккаунтов."""
    if not accounts:
        return "📱 Нет доступных аккаунтов"
    lines = ["📱 <b>Аккаунты для операции:</b>"]
    for acc in accounts[:max_items]:
        suit_bar = "█" * round(acc.suitability_score * 5) + "░" * (
            5 - round(acc.suitability_score * 5)
        )
        status = "✅" if acc.recommended else ("⏳" if acc.is_cooling else "⚠️")
        label = acc.label()
        lines.append(
            f"{status} {label} [{suit_bar}] {int(acc.suitability_score * 100)}%"
        )
    if len(accounts) > max_items:
        lines.append(f"   … и ещё {len(accounts) - max_items} аккаунтов")
    return "\n".join(lines)


def format_risk_assessment(risk: RiskAssessment) -> str:
    """Форматировать оценку риска."""
    lines = [f"🎯 <b>Оценка риска:</b> {risk.summary}"]
    if risk.blockers:
        lines.append("\n🚫 <b>Причины блокировки:</b>")
        for b in risk.blockers:
            lines.append(f"  • {b}")
    if risk.reasons:
        lines.append("\n⚠️ <b>Факторы риска:</b>")
        for r in risk.reasons[:3]:
            lines.append(f"  • {r}")
    if risk.recommendations:
        lines.append("\n💡 <b>Рекомендации:</b>")
        for rec in risk.recommendations[:3]:
            lines.append(f"  • {rec}")
    return "\n".join(lines)


# ── Реализация analyze_accounts ───────────────────────────────────────────────


async def _analyze_accounts_impl(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str,
    account_ids: Optional[list[int]] = None,
) -> list[AccountIntelligence]:
    from services import infra_memory
    from services.flood_engine import get_account_state

    # Запрос аккаунтов из БД (включая health_score для полного анализа)
    if account_ids:
        rows = await pool.fetch(
            """SELECT id, phone, first_name, trust_score, flood_count_7d,
                      cooldown_until, pool, tags, acc_status,
                      (session_str IS NOT NULL AND session_str <> '') AS has_session,
                      COALESCE(health_score, 0.5) AS health_score
               FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE AND id=ANY($2)
               ORDER BY COALESCE(trust_score, 1.0) DESC""",
            owner_id,
            account_ids,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, phone, first_name, trust_score, flood_count_7d,
                      cooldown_until, pool, tags, acc_status,
                      (session_str IS NOT NULL AND session_str <> '') AS has_session,
                      COALESCE(health_score, 0.5) AS health_score
               FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
               ORDER BY COALESCE(trust_score, 1.0) DESC""",
            owner_id,
        )

    now = time.time()
    result: list[AccountIntelligence] = []

    for row in rows:
        acc_id = row["id"]
        trust = float(row["trust_score"] or 1.0)
        flood_7d = int(row["flood_count_7d"] or 0)
        cooldown = row["cooldown_until"]
        acc_status = effective_account_status(
            row["acc_status"],
            has_session=bool(row["has_session"]),
            is_active=True,
        )
        tags = list(row["tags"] or [])
        pool_name = row["pool"]
        health = float(row["health_score"])

        is_cooling = cooldown is not None and cooldown.timestamp() > now
        cooldown_minutes = (
            max(0, int((cooldown.timestamp() - now) / 60)) if is_cooling else 0
        )

        # In-memory риск из flood_engine
        try:
            mem_state = get_account_state(acc_id)
            flood_risk = mem_state.risk_score if mem_state else 0.0
        except Exception:
            flood_risk = 0.0

        # Reliability из infra_memory (историческая успешность)
        mem_score = infra_memory.get_account_score(acc_id, action_type)
        mem_summary = infra_memory.get_account_summary(acc_id, action_type)
        mem_successes = mem_summary.get("successes", 0)
        mem_failures = mem_summary.get("failures", 0)

        # ── Risk Score (0=безопасно, 1=опасно) ──
        risk_factors = []

        # Флуд-активность
        flood_risk_component = min(1.0, flood_7d / 15.0)
        risk_factors.append(flood_risk_component * 0.35)

        # In-memory риск
        risk_factors.append(flood_risk * 0.25)

        # Низкое доверие
        trust_risk = max(0.0, (0.6 - trust) / 0.6) if trust < 0.6 else 0.0
        risk_factors.append(trust_risk * 0.25)

        # Проблемный статус
        status_risk = 0.0
        if acc_status in ("spamblock", "banned", "no_session"):
            status_risk = 1.0
        elif acc_status in ("restricted", "deactivated"):
            status_risk = 0.7
        risk_factors.append(status_risk * 0.15)

        risk_score = sum(risk_factors)
        risk_score = max(0.0, min(1.0, risk_score))

        # ── Reliability Score (из infra_memory) ──
        reliability_score = mem_score  # уже 0.0–1.0

        # ── Suitability Score — итоговая пригодность для операции ──
        # Комбинация: trust + health + низкий риск + историческая надёжность
        suitability = (
            trust * 0.30
            + health * 0.15
            + (1.0 - risk_score) * 0.30
            + reliability_score * 0.25
        )
        suitability = max(0.0, min(1.0, suitability))

        # Решение — рекомендовать или нет
        skip_reason = ""
        if acc_status in ("spamblock", "banned", "deactivated", "no_session"):
            skip_reason = f"статус: {acc_status}"
        elif is_cooling:
            skip_reason = f"кулдаун ещё {cooldown_minutes} мин"
        elif risk_score > 0.8:
            skip_reason = f"высокий риск ({int(risk_score * 100)}%)"
        elif trust < 0.2:
            skip_reason = f"очень низкое доверие ({int(trust * 100)}%)"

        recommended = not skip_reason

        result.append(
            AccountIntelligence(
                account_id=acc_id,
                phone=row["phone"] or "",
                first_name=row["first_name"] or "",
                suitability_score=round(suitability, 3),
                risk_score=round(risk_score, 3),
                reliability_score=round(reliability_score, 3),
                trust_score=trust,
                flood_count_7d=flood_7d,
                is_cooling=is_cooling,
                cooldown_minutes=cooldown_minutes,
                pool=pool_name,
                tags=tags,
                memory_successes=mem_successes,
                memory_failures=mem_failures,
                recommended=recommended,
                skip_reason=skip_reason,
            )
        )

    result.sort(key=lambda a: a.suitability_score, reverse=True)
    return result


# ── Реализация analyze_proxies ────────────────────────────────────────────────


async def _analyze_proxies_impl(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[ProxyIntelligence]:
    rows = await pool.fetch(
        """SELECT up.id, up.label, up.proxy_type,
               COUNT(q.id) AS total_checks,
               COUNT(q.id) FILTER (WHERE q.success) AS ok_checks,
               COUNT(q.id) FILTER (WHERE NOT q.success AND q.checked_at > NOW() - INTERVAL '24 hours') AS recent_fails,
               AVG(q.latency_ms) FILTER (WHERE q.success) AS avg_latency
           FROM user_proxies up
           LEFT JOIN proxy_quality_log q ON q.proxy_id = up.id
               AND q.checked_at > NOW() - INTERVAL '7 days'
           WHERE up.owner_id = $1
           GROUP BY up.id, up.label, up.proxy_type""",
        owner_id,
    )

    result: list[ProxyIntelligence] = []
    for row in rows:
        total = int(row["total_checks"] or 0)
        ok = int(row["ok_checks"] or 0)
        recent_fails = int(row["recent_fails"] or 0)
        latency = float(row["avg_latency"] or 0.0)

        success_rate = ok / total if total > 0 else 0.5

        # Quality Score: успешность + скорость
        quality = success_rate
        if total >= 5:
            if latency > 0:
                latency_factor = max(0.0, 1.0 - latency / 3000.0)  # 0ms=1.0, 3000ms=0.0
                quality = quality * 0.7 + latency_factor * 0.3
        else:
            quality = 0.5  # неизвестно

        # Risk Score: недавние сбои + общая ненадёжность
        if total == 0:
            risk = 0.5
        else:
            fail_rate = 1.0 - success_rate
            recent_fail_factor = min(1.0, recent_fails / 3.0)
            risk = fail_rate * 0.6 + recent_fail_factor * 0.4

        quality = max(0.0, min(1.0, quality))
        risk = max(0.0, min(1.0, risk))

        recommended = quality >= 0.5 and risk < 0.6

        result.append(
            ProxyIntelligence(
                proxy_id=row["id"],
                label=row["label"] or f"proxy#{row['id']}",
                proxy_type=row["proxy_type"] or "socks5",
                quality_score=round(quality, 3),
                risk_score=round(risk, 3),
                success_rate=round(success_rate, 3),
                avg_latency_ms=round(latency, 1),
                total_checks=total,
                recent_failures=recent_fails,
                recommended=recommended,
            )
        )

    result.sort(key=lambda p: p.quality_score, reverse=True)
    return result


# ── Реализация assess_risk ────────────────────────────────────────────────────


async def _assess_risk_impl(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
) -> RiskAssessment:
    from services import infra_pressure

    pressure_data = await infra_pressure.compute_pressure(pool, owner_id)
    pressure = pressure_data.get("score", 0)
    pressure_data.get("breakdown", {})

    # Данные об аккаунтах
    acc_row = await pool.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE is_active) AS total,
               COUNT(*) FILTER (WHERE is_active
                   AND (cooldown_until IS NULL OR cooldown_until < NOW())
                   AND session_str IS NOT NULL
                   AND session_str <> ''
                   AND COALESCE(acc_status, 'active') NOT IN ('spamblock', 'banned', 'deactivated')
               ) AS available,
               COUNT(*) FILTER (WHERE is_active AND COALESCE(trust_score,1.0) < 0.4) AS low_trust,
               AVG(COALESCE(trust_score,1.0)) FILTER (WHERE is_active) AS avg_trust,
               COUNT(*) FILTER (WHERE is_active AND COALESCE(flood_count_7d,0) > 10) AS high_flood
           FROM tg_accounts WHERE owner_id=$1""",
        owner_id,
    )
    total_accs = int(acc_row["total"] or 0)
    available_accs = int(acc_row["available"] or 0)
    low_trust_accs = int(acc_row["low_trust"] or 0)
    avg_trust = float(acc_row["avg_trust"] or 1.0)
    high_flood_accs = int(acc_row["high_flood"] or 0)

    # Вычисляем score риска (0–100)
    risk_score = 0
    reasons: list[str] = []
    blockers: list[str] = []
    recommendations: list[str] = []

    # 1. Pressure
    if pressure >= 85:
        risk_score += 40
        blockers.append(f"Давление инфраструктуры критическое ({pressure}/100)")
    elif pressure >= 70:
        risk_score += 25
        reasons.append(f"Высокое давление инфраструктуры ({pressure}/100)")
        recommendations.append("Снизьте количество одновременных операций")
    elif pressure >= 50:
        risk_score += 10
        reasons.append(f"Умеренное давление ({pressure}/100)")

    # 2. Доступность аккаунтов
    max_items_per_acc = _MAX_ITEMS_PER_ACCOUNT.get(op_type, 100)
    max_capacity = available_accs * max_items_per_acc if available_accs > 0 else 0

    if available_accs == 0:
        risk_score += 45
        blockers.append("Нет доступных аккаунтов")
    elif item_count > max_capacity:
        overflow_factor = item_count / max(max_capacity, 1)
        if overflow_factor > 3:
            risk_score += 30
            reasons.append(
                f"Объём {item_count} сильно превышает ёмкость {max_capacity} "
                f"({available_accs} акк × {max_items_per_acc})"
            )
            recommendations.append(
                f"Уменьшите объём до {max_capacity} или добавьте {item_count // max_items_per_acc - available_accs + 1} аккаунтов"
            )
        elif overflow_factor > 1.5:
            risk_score += 15
            reasons.append(
                f"Объём {item_count} превышает комфортную ёмкость {max_capacity}"
            )

    # 3. Низкое доверие
    if total_accs > 0 and low_trust_accs / total_accs > 0.5:
        risk_score += 20
        reasons.append(f"{low_trust_accs} из {total_accs} аккаунтов с низким доверием")
        recommendations.append("Разогрейте проблемные аккаунты")
    elif avg_trust < 0.5:
        risk_score += 10
        reasons.append(f"Среднее доверие аккаунтов низкое ({avg_trust:.0%})")

    # 4. Высокая флуд-активность
    if high_flood_accs > 0 and total_accs > 0:
        flood_ratio = high_flood_accs / total_accs
        if flood_ratio > 0.5:
            risk_score += 15
            reasons.append(f"{high_flood_accs} аккаунтов с высокой флуд-активностью")
        elif flood_ratio > 0.2:
            risk_score += 7
            reasons.append(f"{high_flood_accs} аккаунтов с повышенной флуд-активностью")

    # 5. Специфичный риск для типа операции
    if op_type == "strike" and item_count > 50:
        risk_score += 10
        reasons.append(f"Strike с {item_count} целями — повышенный риск детекции")
        recommendations.append("Используйте staggered timing и минимум 3 аккаунта")
    elif op_type == "dm_campaign" and item_count > 200:
        risk_score += 8
        reasons.append(f"DM-кампания на {item_count} пользователей — риск PEER_FLOOD")
        recommendations.append("Распределите по времени, используйте разные аккаунты")
    elif op_type == "bulk_join" and item_count > 100:
        risk_score += 8
        reasons.append(f"Bulk join {item_count} каналов — риск ограничений Telegram")

    risk_score = max(0, min(100, risk_score))

    # Определяем уровень
    if blockers or risk_score >= 80:
        level, emoji = "critical", "🔴"
        safe_to_proceed = not blockers
    elif risk_score >= 55:
        level, emoji = "high", "🟠"
        safe_to_proceed = True
    elif risk_score >= 30:
        level, emoji = "medium", "🟡"
        safe_to_proceed = True
    else:
        level, emoji = "low", "🟢"
        safe_to_proceed = True

    if blockers:
        safe_to_proceed = False

    # Добавляем рекомендацию по умолчанию если нет
    if not recommendations and level in ("high", "critical") and not blockers:
        recommendations.append("Рассмотрите выполнение операции позже или по частям")

    return RiskAssessment(
        level=level,
        level_emoji=emoji,
        score=risk_score,
        reasons=reasons,
        blockers=blockers,
        recommendations=recommendations,
        safe_to_proceed=safe_to_proceed,
    )


# ── Реализация predict_operation ─────────────────────────────────────────────


async def _predict_impl(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
    account_count: Optional[int],
) -> OperationPrediction:
    from services import infra_memory

    # Получить количество доступных аккаунтов
    if account_count is None:
        row = await pool.fetchrow(
            """SELECT COUNT(*) AS cnt FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND (cooldown_until IS NULL OR cooldown_until < NOW())""",
            owner_id,
        )
        account_count = int(row["cnt"] or 0) if row else 0

    account_count = max(1, account_count)

    # Базовые тайминги
    t_min, t_max = _OP_TIMINGS.get(op_type, _OP_TIMINGS["default"])

    # Корректировка по памяти операций
    all_acc_rows = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", owner_id
    )
    acc_ids = [r["id"] for r in all_acc_rows]
    if acc_ids:
        ranked = infra_memory.rank_accounts_by_memory(acc_ids, op_type)
        ranked_slice = ranked[:account_count]
        avg_memory_score = (
            sum(s for _, s in ranked_slice) / len(ranked_slice) if ranked_slice else 0.5
        )
    else:
        avg_memory_score = 0.5

    # Базовая вероятность успеха
    base_rate = _BASE_SUCCESS_RATES.get(op_type, 0.80)
    # Корректировка по памяти: хороший track record повышает, плохой снижает
    memory_adjustment = (avg_memory_score - 0.5) * 0.20  # ±10%
    success_probability = max(0.30, min(0.98, base_rate + memory_adjustment))

    # Время выполнения — используем реальные данные из infra_memory если есть
    historical_duration: Optional[float] = None
    if acc_ids:
        durations = [
            infra_memory.get_account_avg_duration(acc_id, op_type)
            for acc_id in acc_ids[:account_count]
        ]
        valid = [d for d in durations if d is not None and d > 0]
        if len(valid) >= 2:
            historical_duration = sum(valid) / len(valid)

    if historical_duration and historical_duration > 0:
        # Используем реальное среднее + ±20% диапазон
        t_hist_min = historical_duration * 0.8
        t_hist_max = historical_duration * 1.2
        # Смешиваем с базовыми константами (70% история, 30% baseline)
        t_min = t_hist_min * 0.7 + t_min * 0.3
        t_max = t_hist_max * 0.7 + t_max * 0.3

    # Параллельное выполнение с аккаунтами
    items_per_account = item_count / account_count
    effective_items = max(1, item_count / account_count)
    t_min_total = int(effective_items * t_min / 60)
    t_max_total = int(effective_items * t_max / 60)
    t_avg = (t_min_total + t_max_total) // 2

    # Ожидаемые результаты
    expected_ok = int(item_count * success_probability)
    expected_fail = item_count - expected_ok

    # Уверенность прогноза
    total_history = sum(
        1 for _, s in (ranked[:account_count] if acc_ids else []) if s != 0.5
    )
    if total_history >= account_count * 5:
        confidence = "high"
    elif total_history >= account_count:
        confidence = "medium"
    else:
        confidence = "low"

    return OperationPrediction(
        op_type=op_type,
        item_count=item_count,
        account_count=account_count,
        estimated_minutes=t_avg,
        estimated_minutes_min=t_min_total,
        estimated_minutes_max=t_max_total,
        success_probability=round(success_probability, 3),
        expected_success_items=expected_ok,
        expected_failed_items=expected_fail,
        items_per_account=round(items_per_account, 1),
        confidence=confidence,
    )


def _fallback_prediction(
    op_type: str, item_count: int, account_count: int
) -> OperationPrediction:
    t_min, t_max = _OP_TIMINGS.get(op_type, _OP_TIMINGS["default"])
    effective_items = max(1, item_count / max(account_count, 1))
    t_avg = int(effective_items * (t_min + t_max) / 2 / 60)
    rate = _BASE_SUCCESS_RATES.get(op_type, 0.80)
    return OperationPrediction(
        op_type=op_type,
        item_count=item_count,
        account_count=account_count,
        estimated_minutes=t_avg,
        estimated_minutes_min=int(effective_items * t_min / 60),
        estimated_minutes_max=int(effective_items * t_max / 60),
        success_probability=rate,
        expected_success_items=int(item_count * rate),
        expected_failed_items=item_count - int(item_count * rate),
        items_per_account=round(item_count / max(account_count, 1), 1),
        confidence="low",
    )


# ── Реализация get_pre_launch_intelligence ────────────────────────────────────


async def _pre_launch_impl(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
    account_ids: Optional[list[int]],
) -> PreLaunchIntelligence:
    from services import infra_pressure

    # Параллельный сбор данных (включая анализ прокси)
    accs_task = asyncio.create_task(
        analyze_accounts(pool, owner_id, op_type, account_ids)
    )
    risk_task = asyncio.create_task(assess_risk(pool, owner_id, op_type, item_count))
    pressure_task = asyncio.create_task(infra_pressure.compute_pressure(pool, owner_id))
    proxies_task = asyncio.create_task(analyze_proxies(pool, owner_id))

    accs_list, risk, pressure_data, proxies_list = await asyncio.gather(
        accs_task, risk_task, pressure_task, proxies_task
    )

    recommended_accs = [a for a in accs_list if a.recommended]
    acc_count = len(recommended_accs) if recommended_accs else max(1, len(accs_list))
    prediction = await predict_operation(pool, owner_id, op_type, item_count, acc_count)

    pressure_score = pressure_data.get("score", 0)
    pressure_emoji = pressure_data.get("level_emoji", "🟢")
    pressure_label = pressure_data.get("level_label", "Норма")

    recommended_proxies = [p for p in proxies_list if p.recommended]

    intel = PreLaunchIntelligence(
        op_type=op_type,
        item_count=item_count,
        owner_id=owner_id,
        risk=risk,
        prediction=prediction,
        recommended_accounts=recommended_accs,
        all_accounts=accs_list,
        recommended_proxies=recommended_proxies,
        all_proxies=proxies_list,
        pressure_score=pressure_score,
        pressure_label=pressure_label,
        pressure_emoji=pressure_emoji,
    )

    # Итоговое решение
    if not risk.safe_to_proceed:
        intel.go_decision = False
        intel.go_reason = risk.blockers[0] if risk.blockers else "Критический риск"
    elif len(recommended_accs) == 0 and accs_list:
        # Аккаунты есть, но все неподходящие
        intel.go_decision = True
        intel.warning_text = "Все доступные аккаунты имеют высокий риск — операция возможна, но нежелательна"
    elif pressure_score >= 70:
        intel.go_decision = True
        intel.warning_text = f"Повышенное давление ({pressure_score}/100) — операция выполнится, но медленнее обычного"
    elif risk.level in ("high", "critical") and risk.safe_to_proceed:
        intel.go_decision = True
        intel.warning_text = (
            f"Высокий риск операции: {risk.reasons[0]}"
            if risk.reasons
            else "Высокий риск"
        )

    # Ecosystem context: проверяем критические экосистемы владельца
    try:
        from services import ecosystem_brain as _eb

        _ecosystems = await _eb.list_ecosystems(pool, owner_id)
        for _eco in _ecosystems:
            _h = _eco.get("health_score") or 1.0
            _p = _eco.get("pressure_score") or 0
            _risk_level = _eco.get("risk_level") or "low"
            if _h < 0.35:
                _eco_warn = f"Экосистема «{_eco['name']}»: здоровье критическое ({int(_h * 100)}%)"
                if not intel.warning_text:
                    intel.warning_text = _eco_warn
                break
            if _p >= 80:
                _eco_warn = f"Экосистема «{_eco['name']}»: давление {_p}/100"
                if not intel.warning_text:
                    intel.warning_text = _eco_warn
                break
            if _risk_level == "critical":
                _eco_warn = f"Экосистема «{_eco['name']}»: критический риск"
                if not intel.warning_text:
                    intel.warning_text = _eco_warn
                break
    except Exception as e:
        log.warning(
            "intelligence_engine: ecosystem check failed owner=%d: %s", owner_id, e
        )

    return intel


# ── Вспомогательная функция для flood_engine.get_account_state ───────────────


def get_account_state(account_id: int):
    """Безопасный wrapper для flood_engine.get_account_state."""
    try:
        from services.flood_engine import get_account_state as _gas

        return _gas(account_id)
    except Exception:
        return None
