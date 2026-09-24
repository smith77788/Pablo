"""Аналитика аудитории: сегменты, удержание, предсказание вовлечённости.

Ключ сущности — bot_id. Раньше весь модуль обращался к колонке `channel_id`,
которой нет ни в bot_users, ни в user_activity, ни в content_performance: там
bot_id. Каждый запрос модуля падал с UndefinedColumnError, ошибку глотал
log_exc_swallow, и наружу это выходило нулями — «аудитории нет». Проверяет
tests/test_sql_runs_on_real_schema_postgres.py.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class AudienceOverview:
    bot_id: int
    owner_id: int
    total_subscribers: int = 0
    active_users: int = 0
    active_rate: float = 0.0
    avg_session_duration_min: float = 0.0
    peak_hour: int = 0
    peak_day: str = ""
    retention_7d: float = 0.0
    retention_30d: float = 0.0
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AudienceSegment:
    name: str
    user_count: int = 0
    percentage: float = 0.0
    avg_engagement: float = 0.0
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class EngagementPrediction:
    bot_id: int
    predicted_er: float = 0.0
    confidence: float = 0.0
    best_post_hour: int = 0
    best_post_day: str = ""
    recommended_content_types: list[str] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AudienceInsights:
    bot_id: int
    growth_trend: str = "stable"
    growth_rate_pct: float = 0.0
    churn_risk_pct: float = 0.0
    top_activity_hours: list[int] = field(default_factory=list)
    content_preferences: list[str] = field(default_factory=list)
    audience_quality_score: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Core Functions ────────────────────────────────────────────────────────────


async def analyze_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
) -> AudienceOverview | None:
    """
    Комплексный анализ аудитории бота.

    Собирает данные о:
    - Общем количестве подписчиков
    - Активных пользователях (за 7 дней)
    - Средней продолжительности сессии
    - Пиках активности (часы и дни)
    - Retention rate (7д и 30д)
    """
    try:
        # ── 1. Базовая статистика ─────────────────────────────────────────────
        total_subscribers = await _get_subscriber_count(pool, bot_id)
        active_users = await _get_active_users(pool, bot_id, days=7)

        # ── 2. Активность по часам ───────────────────────────────────────────
        peak_hour, peak_day = await _get_peak_activity(pool, bot_id)

        # ── 3. Retention rate ─────────────────────────────────────────────────
        retention_7d = await _calc_retention(pool, bot_id, days=7)
        retention_30d = await _calc_retention(pool, bot_id, days=30)

        # ── 4. Средняя продолжительность сессии ──────────────────────────────
        avg_session = await _get_avg_session_duration(pool, bot_id)

        # ── 5. Активная аудитория ─────────────────────────────────────────────
        active_rate = (active_users / total_subscribers * 100) if total_subscribers > 0 else 0.0

        overview = AudienceOverview(
            bot_id=bot_id,
            owner_id=owner_id,
            total_subscribers=total_subscribers,
            active_users=active_users,
            active_rate=round(active_rate, 2),
            avg_session_duration_min=round(avg_session, 1),
            peak_hour=peak_hour,
            peak_day=peak_day,
            retention_7d=round(retention_7d, 2),
            retention_30d=round(retention_30d, 2),
        )

        log.info(
            "audience_analytics: analyze_audience channel=%s subscribers=%s active=%s",
            bot_id, total_subscribers, active_users,
        )
        return overview

    except Exception as exc:
        log.warning("analyze_audience failed for bot_id=%s: %s", bot_id, exc)
        return None


async def segment_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
) -> list[AudienceSegment]:
    """
    Сегментация аудитории по поведенческим характеристикам.

    Сегменты:
    - "Champions" — высокоактивные, высокий ER
    - "Loyal" — стабильная активность, средний ER
    - "At Risk" — снижение активности
    - "Dormant" — неактивные 14+ дней
    - "New" — присоединились за последние 7 дней
    """
    try:
        users = await _fetch_user_activity(pool, bot_id)
        if not users:
            return []

        total = len(users)
        now = datetime.now(timezone.utc)

        segments: list[AudienceSegment] = []

        # ── Champions ─────────────────────────────────────────────────────────
        champions = [
            u for u in users
            if u["days_active_30d"] >= 20 and u["total_engagements"] >= 50
        ]
        if champions:
            avg_er = sum(u.get("engagement_rate", 0) for u in champions) / len(champions)
            segments.append(AudienceSegment(
                name="Champions",
                user_count=len(champions),
                percentage=round(len(champions) / total * 100, 1),
                avg_engagement=round(avg_er, 2),
                description="Самые активные и вовлечённые подписчики",
                tags=["high_value", "advocates"],
            ))

        # ── Loyal ─────────────────────────────────────────────────────────────
        loyal = [
            u for u in users
            if u["days_active_30d"] >= 10 and u["total_engagements"] >= 20
            and u not in champions
        ]
        if loyal:
            avg_er = sum(u.get("engagement_rate", 0) for u in loyal) / len(loyal)
            segments.append(AudienceSegment(
                name="Loyal",
                user_count=len(loyal),
                percentage=round(len(loyal) / total * 100, 1),
                avg_engagement=round(avg_er, 2),
                description="Стабильные подписчики с регулярной активностью",
                tags=["regular", "engaged"],
            ))

        # ── At Risk ───────────────────────────────────────────────────────────
        at_risk = [
            u for u in users
            if u["days_active_30d"] >= 3 and u["days_active_30d"] < 10
            and u.get("last_active")
            and (now - u["last_active"]).days < 14
        ]
        if at_risk:
            avg_er = sum(u.get("engagement_rate", 0) for u in at_risk) / len(at_risk)
            segments.append(AudienceSegment(
                name="At Risk",
                user_count=len(at_risk),
                percentage=round(len(at_risk) / total * 100, 1),
                avg_engagement=round(avg_er, 2),
                description="Подписчики со снижающейся активностью",
                tags=["declining", "needs_reactivation"],
            ))

        # ── Dormant ───────────────────────────────────────────────────────────
        dormant = [
            u for u in users
            if not u.get("last_active") or (now - u["last_active"]).days >= 14
        ]
        if dormant:
            segments.append(AudienceSegment(
                name="Dormant",
                user_count=len(dormant),
                percentage=round(len(dormant) / total * 100, 1),
                avg_engagement=0.0,
                description="Неактивные подписчики (14+ дней без активности)",
                tags=["inactive", "churned"],
            ))

        # ── New ───────────────────────────────────────────────────────────────
        new_users = [
            u for u in users
            if u.get("first_seen")
            and (now - u["first_seen"]).days <= 7
        ]
        if new_users:
            avg_er = sum(u.get("engagement_rate", 0) for u in new_users) / len(new_users) if new_users else 0.0
            segments.append(AudienceSegment(
                name="New",
                user_count=len(new_users),
                percentage=round(len(new_users) / total * 100, 1),
                avg_engagement=round(avg_er, 2),
                description="Новые подписчики за последние 7 дней",
                tags=["new", "onboarding"],
            ))

        log.info(
            "audience_analytics: segment_audience channel=%s segments=%d",
            bot_id, len(segments),
        )
        return segments

    except Exception as exc:
        log.warning("segment_audience failed for bot_id=%s: %s", bot_id, exc)
        return []


async def predict_engagement(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
) -> EngagementPrediction | None:
    """
    Прогнозирование вовлечённости на основе исторических данных.

    Учитывает:
    - Исторический ER по часам и дням недели
    - Тренды роста/снижения активности
    - Влияние типов контента
    - Сезонные паттерны
    """
    try:
        # ── 1. Собираем исторические данные ───────────────────────────────────
        hourly_er = await _get_hourly_engagement(pool, bot_id)
        daily_er = await _get_daily_engagement(pool, bot_id)
        content_er = await _get_content_type_engagement(pool, bot_id)

        # ── 2. Определяем лучшее время ───────────────────────────────────────
        best_hour = 0
        best_er_hourly = 0.0
        if hourly_er:
            best_hour = max(hourly_er, key=hourly_er.get)
            best_er_hourly = hourly_er[best_hour]

        best_day = "Пн"
        best_er_daily = 0.0
        if daily_er:
            best_day_idx = max(daily_er, key=daily_er.get)
            best_day = _DAY_NAMES[best_day_idx] if best_day_idx < len(_DAY_NAMES) else "Пн"
            best_er_daily = daily_er[best_day_idx]

        # ── 3. Прогнозируемый ER ──────────────────────────────────────────────
        if hourly_er and daily_er:
            avg_hourly = sum(hourly_er.values()) / len(hourly_er)
            avg_daily = sum(daily_er.values()) / len(daily_er)
            predicted_er = (avg_hourly * 0.4 + avg_daily * 0.4 + best_er_hourly * 0.2) * 100
        elif hourly_er:
            predicted_er = sum(hourly_er.values()) / len(hourly_er) * 100
        else:
            predicted_er = 0.0

        # ── 4. Уверенность прогноза ──────────────────────────────────────────
        data_points = len(hourly_er) + len(daily_er) + len(content_er)
        confidence = min(0.95, data_points / 100)

        # ── 5. Рекомендации по контенту ──────────────────────────────────────
        recommended = []
        if content_er:
            sorted_content = sorted(content_er.items(), key=lambda x: x[1], reverse=True)
            recommended = [ct for ct, _ in sorted_content[:3]]

        # ── 6. Факторы влияния ───────────────────────────────────────────────
        factors = []
        if best_er_hourly > 0.05:
            factors.append(f"Пик активности в {best_hour}:00")
        if best_er_daily > 0.05:
            factors.append(f"Лучший день: {best_day}")
        if content_er:
            top_ct = max(content_er, key=content_er.get)
            factors.append(f"Топ-формат: {top_ct}")

        prediction = EngagementPrediction(
            bot_id=bot_id,
            predicted_er=round(predicted_er, 2),
            confidence=round(confidence, 2),
            best_post_hour=best_hour,
            best_post_day=best_day,
            recommended_content_types=recommended,
            factors=factors,
        )

        log.info(
            "audience_analytics: predict_engagement channel=%s predicted_er=%.2f%%",
            bot_id, predicted_er,
        )
        return prediction

    except Exception as exc:
        log.warning("predict_engagement failed for bot_id=%s: %s", bot_id, exc)
        return None


async def get_audience_insights(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
) -> AudienceInsights | None:
    """
    Комплексные инсайты аудитории с рекомендациями.

    Включает:
    - Тренд роста аудитории
    - Риск оттока
    - Предпочтения контента
    - Качество аудитории
    - Персонализированные рекомендации
    """
    try:
        # ── 1. Тренд роста ────────────────────────────────────────────────────
        growth_trend, growth_rate = await _calc_growth_trend(pool, bot_id)

        # ── 2. Риск оттока ───────────────────────────────────────────────────
        churn_risk = await _calc_churn_risk(pool, bot_id)

        # ── 3. Пики активности ───────────────────────────────────────────────
        peak_hours = await _get_top_activity_hours(pool, bot_id, limit=3)

        # ── 4. Предпочтения контента ─────────────────────────────────────────
        content_prefs = await _get_content_preferences(pool, bot_id)

        # ── 5. Качество аудитории ────────────────────────────────────────────
        quality_score = await _calc_audience_quality(pool, bot_id)

        # ── 6. Рекомендации ──────────────────────────────────────────────────
        recommendations = _generate_insight_recommendations(
            growth_trend=growth_trend,
            growth_rate=growth_rate,
            churn_risk=churn_risk,
            peak_hours=peak_hours,
            content_prefs=content_prefs,
            quality_score=quality_score,
        )

        insights = AudienceInsights(
            bot_id=bot_id,
            growth_trend=growth_trend,
            growth_rate_pct=round(growth_rate, 2),
            churn_risk_pct=round(churn_risk, 2),
            top_activity_hours=peak_hours,
            content_preferences=content_prefs,
            audience_quality_score=round(quality_score, 2),
            recommendations=recommendations,
        )

        log.info(
            "audience_analytics: get_audience_insights channel=%s quality=%.1f churn=%.1f%%",
            bot_id, quality_score, churn_risk,
        )
        return insights

    except Exception as exc:
        log.warning("get_audience_insights failed for bot_id=%s: %s", bot_id, exc)
        return None


# ── DB Helpers ────────────────────────────────────────────────────────────────


async def _get_subscriber_count(pool: asyncpg.Pool, bot_id: int) -> int:
    """Получить количество подписчиков бота."""
    try:
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM bot_users WHERE bot_id = $1",
            bot_id,
        )
        return int(row["cnt"]) if row else 0
    except Exception:
        log_exc_swallow(log, "_get_subscriber_count")
        return 0


async def _get_active_users(pool: asyncpg.Pool, bot_id: int, days: int = 7) -> int:
    """Получить количество активных пользователей за N дней."""
    try:
        row = await pool.fetchrow(
            """
            SELECT COUNT(DISTINCT user_id) AS cnt
            FROM user_activity
            WHERE bot_id = $1
              AND last_seen >= NOW() - ($2 || ' days')::INTERVAL
            """,
            bot_id,
            str(days),
        )
        return int(row["cnt"]) if row else 0
    except Exception:
        log_exc_swallow(log, "_get_active_users")
        return 0


async def _get_peak_activity(
    pool: asyncpg.Pool, bot_id: int
) -> tuple[int, str]:
    """Определить пиковые часы и дни активности."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM last_seen)::INT AS hour,
                EXTRACT(DOW FROM last_seen)::INT AS dow,
                COUNT(*) AS cnt
            FROM user_activity
            WHERE bot_id = $1 AND last_seen IS NOT NULL
            GROUP BY 1, 2
            ORDER BY cnt DESC
            LIMIT 10
            """,
            bot_id,
        )
        if not rows:
            return 0, "Пн"

        hour_counter: Counter[int] = Counter()
        dow_counter: Counter[int] = Counter()
        for r in rows:
            hour_counter[r["hour"]] += r["cnt"]
            dow_counter[r["dow"]] += r["cnt"]

        peak_hour = hour_counter.most_common(1)[0][0] if hour_counter else 0
        peak_dow = dow_counter.most_common(1)[0][0] if dow_counter else 0

        pg_dow_to_mon = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
        peak_day = _DAY_NAMES[pg_dow_to_mon.get(peak_dow, 0)]

        return peak_hour, peak_day
    except Exception:
        log_exc_swallow(log, "_get_peak_activity")
        return 0, "Пн"


async def _calc_retention(
    pool: asyncpg.Pool, bot_id: int, days: int = 7
) -> float:
    """Рассчитать retention rate за N дней."""
    try:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(DISTINCT user_id) AS total_users,
                COUNT(DISTINCT CASE
                    WHEN last_seen >= NOW() - ($2 || ' days')::INTERVAL
                         AND first_seen < NOW() - ($2 || ' days')::INTERVAL
                    THEN user_id
                END) AS retained_users
            FROM user_activity
            WHERE bot_id = $1
            """,
            bot_id,
            str(days),
        )
        if row and row["total_users"] and row["total_users"] > 0:
            return (row["retained_users"] / row["total_users"]) * 100
        return 0.0
    except Exception:
        log_exc_swallow(log, "_calc_retention")
        return 0.0


async def _get_avg_session_duration(pool: asyncpg.Pool, bot_id: int) -> float:
    """Средняя продолжительность сессии в минутах."""
    try:
        row = await pool.fetchrow(
            """
            SELECT AVG(
                EXTRACT(EPOCH FROM (last_seen - first_seen)) / 60
            ) AS avg_duration
            FROM user_activity
            WHERE bot_id = $1
              AND first_seen IS NOT NULL
              AND last_seen IS NOT NULL
              AND last_seen > first_seen
            """,
            bot_id,
        )
        return float(row["avg_duration"]) if row and row["avg_duration"] else 0.0
    except Exception:
        log_exc_swallow(log, "_get_avg_session_duration")
        return 0.0


async def _fetch_user_activity(
    pool: asyncpg.Pool, bot_id: int
) -> list[dict[str, Any]]:
    """Получить данные активности пользователей для сегментации."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                user_id,
                last_seen,
                first_seen,
                EXTRACT(DAY FROM NOW() - last_seen)::INT AS days_since_last,
                EXTRACT(DAY FROM NOW() - first_seen)::INT AS days_since_first
            FROM user_activity
            WHERE bot_id = $1
            """,
            bot_id,
        )
        result = []
        for r in rows:
            days_active = max(0, 30 - (r["days_since_last"] or 30))
            engagements = await _get_user_engagements(pool, r["user_id"], bot_id)
            result.append({
                "user_id": r["user_id"],
                "last_seen": r["last_seen"],
                "first_seen": r["first_seen"],
                "days_active_30d": days_active,
                "total_engagements": engagements,
                "engagement_rate": engagements / 30 if engagements > 0 else 0.0,
            })
        return result
    except Exception:
        log_exc_swallow(log, "_fetch_user_activity")
        return []


async def _get_user_engagements(
    pool: asyncpg.Pool, user_id: int, bot_id: int
) -> int:
    """Получить количество взаимодействий пользователя за 30 дней."""
    try:
        row = await pool.fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM user_activity
            WHERE user_id = $1
              AND bot_id = $2
              AND last_seen >= NOW() - INTERVAL '30 days'
            """,
            user_id,
            bot_id,
        )
        return int(row["cnt"]) if row else 0
    except Exception:
        log_exc_swallow(log, "_get_user_engagements")
        return 0


async def _get_hourly_engagement(
    pool: asyncpg.Pool, bot_id: int
) -> dict[int, float]:
    """Получить средний ER по часам."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM published_at)::INT AS hour,
                AVG(engagement_rate) AS avg_er
            FROM content_performance
            WHERE bot_id = $1
              AND views > 0
            GROUP BY 1
            ORDER BY 1
            """,
            bot_id,
        )
        return {r["hour"]: float(r["avg_er"]) for r in rows if r["hour"] is not None}
    except Exception:
        log_exc_swallow(log, "_get_hourly_engagement")
        return {}


async def _get_daily_engagement(
    pool: asyncpg.Pool, bot_id: int
) -> dict[int, float]:
    """Получить средний ER по дням недели."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                EXTRACT(DOW FROM published_at)::INT AS dow,
                AVG(engagement_rate) AS avg_er
            FROM content_performance
            WHERE bot_id = $1
              AND views > 0
            GROUP BY 1
            ORDER BY 1
            """,
            bot_id,
        )
        return {r["dow"]: float(r["avg_er"]) for r in rows if r["dow"] is not None}
    except Exception:
        log_exc_swallow(log, "_get_daily_engagement")
        return {}


async def _get_content_type_engagement(
    pool: asyncpg.Pool, bot_id: int
) -> dict[str, float]:
    """Получить средний ER по типам контента."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                content_type,
                AVG(engagement_rate) AS avg_er
            FROM content_performance
            WHERE bot_id = $1
              AND views > 0
            GROUP BY content_type
            HAVING COUNT(*) >= 2
            ORDER BY avg_er DESC
            LIMIT 5
            """,
            bot_id,
        )
        return {r["content_type"]: float(r["avg_er"]) for r in rows if r["content_type"]}
    except Exception:
        log_exc_swallow(log, "_get_content_type_engagement")
        return {}


async def _calc_growth_trend(
    pool: asyncpg.Pool, bot_id: int
) -> tuple[str, float]:
    """Определить тренд роста аудитории."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                DATE_TRUNC('week', first_seen)::DATE AS week,
                COUNT(DISTINCT user_id) AS new_users
            FROM user_activity
            WHERE bot_id = $1
              AND first_seen >= NOW() - INTERVAL '8 weeks'
            GROUP BY 1
            ORDER BY 1
            """,
            bot_id,
        )

        if len(rows) < 2:
            return "unknown", 0.0

        values = [int(r["new_users"]) for r in rows]
        if len(values) < 2:
            return "unknown", 0.0

        recent = values[-1]
        previous = values[-2] if len(values) >= 2 else values[0]

        if previous == 0:
            return "growing" if recent > 0 else "stable", 0.0

        growth = ((recent - previous) / previous) * 100

        if growth > 10:
            trend = "growing"
        elif growth < -10:
            trend = "declining"
        else:
            trend = "stable"

        return trend, growth

    except Exception:
        log_exc_swallow(log, "_calc_growth_trend")
        return "unknown", 0.0


async def _calc_churn_risk(pool: asyncpg.Pool, bot_id: int) -> float:
    """Рассчитать риск оттока аудитории."""
    try:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE last_seen < NOW() - INTERVAL '14 days'
                ) AS inactive_cnt
            FROM user_activity
            WHERE bot_id = $1
            """,
            bot_id,
        )
        if row and row["total"] and row["total"] > 0:
            return (row["inactive_cnt"] / row["total"]) * 100
        return 0.0
    except Exception:
        log_exc_swallow(log, "_calc_churn_risk")
        return 0.0


async def _get_top_activity_hours(
    pool: asyncpg.Pool, bot_id: int, limit: int = 3
) -> list[int]:
    """Получить топ-N часов активности."""
    try:
        rows = await pool.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM last_seen)::INT AS hour,
                COUNT(*) AS cnt
            FROM user_activity
            WHERE bot_id = $1 AND last_seen IS NOT NULL
            GROUP BY 1
            ORDER BY cnt DESC
            LIMIT $2
            """,
            bot_id,
            limit,
        )
        return [r["hour"] for r in rows if r["hour"] is not None]
    except Exception:
        log_exc_swallow(log, "_get_top_activity_hours")
        return []


async def _get_content_preferences(
    pool: asyncpg.Pool, bot_id: int
) -> list[str]:
    """Получить предпочтения контента по ER."""
    try:
        rows = await pool.fetch(
            """
            SELECT content_type, AVG(engagement_rate) AS avg_er
            FROM content_performance
            WHERE bot_id = $1 AND views > 0
            GROUP BY content_type
            HAVING COUNT(*) >= 2
            ORDER BY avg_er DESC
            LIMIT 3
            """,
            bot_id,
        )
        return [r["content_type"] for r in rows if r["content_type"]]
    except Exception:
        log_exc_swallow(log, "_get_content_preferences")
        return []


async def _calc_audience_quality(pool: asyncpg.Pool, bot_id: int) -> float:
    """
    Рассчитать качество аудитории (0-100).

    Факторы:
    - Активная аудитория % (0-30)
    - Retention 7д (0-25)
    - Retention 30д (0-25)
    - Engagement rate (0-20)
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE last_seen >= NOW() - INTERVAL '7 days') AS active_7d,
                COUNT(*) FILTER (WHERE last_seen >= NOW() - INTERVAL '30 days') AS active_30d
            FROM user_activity
            WHERE bot_id = $1
            """,
            bot_id,
        )

        if not row or not row["total"] or row["total"] == 0:
            return 0.0

        total = row["total"]
        active_7d = row["active_7d"]
        active_30d = row["active_30d"]

        score = 0.0

        # Активная аудитория % (0-30)
        active_pct = (active_7d / total) * 100
        score += min(30.0, active_pct * 0.6)

        # Retention 7д (0-25)
        ret_7d = (active_7d / total) * 100
        score += min(25.0, ret_7d * 0.5)

        # Retention 30д (0-25)
        ret_30d = (active_30d / total) * 100
        score += min(25.0, ret_30d * 0.5)

        # Engagement rate (0-20) — из content_performance
        er_row = await pool.fetchrow(
            """
            SELECT AVG(engagement_rate) AS avg_er
            FROM content_performance
            WHERE bot_id = $1 AND views > 0
            """,
            bot_id,
        )
        if er_row and er_row["avg_er"]:
            er_pct = float(er_row["avg_er"]) * 100
            score += min(20.0, er_pct * 2.0)

        return min(100.0, score)

    except Exception:
        log_exc_swallow(log, "_calc_audience_quality")
        return 0.0


def _generate_insight_recommendations(
    growth_trend: str,
    growth_rate: float,
    churn_risk: float,
    peak_hours: list[int],
    content_prefs: list[str],
    quality_score: float,
) -> list[str]:
    """Генерация персонализированных рекомендаций."""
    recs: list[str] = []

    # Рекомендации по росту
    if growth_trend == "declining":
        recs.append(
            "📉 <b>Аудитория сокращается:</b> рассмотрите привлечение через рекламу или кросс-промо"
        )
    elif growth_trend == "growing":
        recs.append(
            f"📈 <b>Аудитория растёт:</b> +{growth_rate:.1f}% за неделю — продолжайте текущую стратегию"
        )

    # Рекомендации по оттоку
    if churn_risk > 30:
        recs.append(
            "⚠️ <b>Высокий риск оттока:</b> запустите реактивационную серию для неактивных"
        )
    elif churn_risk > 15:
        recs.append(
            "🟡 <b>Умеренный отток:</b> добавьте интерактивные элементы (опросы, викторины)"
        )

    # Рекомендации по контенту
    if peak_hours:
        hours_str = ", ".join(f"{h}:00" for h in peak_hours[:3])
        recs.append(
            f"⏰ <b>Оптимальное время публикации:</b> {hours_str}"
        )

    if content_prefs:
        prefs_str = ", ".join(content_prefs[:3])
        recs.append(
            f"📝 <b>Лучшие форматы:</b> {prefs_str}"
        )

    # Рекомендации по качеству
    if quality_score < 30:
        recs.append(
            "🔴 <b>Низкое качество аудитории:</b> проверьте качество трафика и привлекайте целевую аудиторию"
        )
    elif quality_score >= 70:
        recs.append(
            "🟢 <b>Отличное качество аудитории:</b> высокий retention и активность"
        )

    return recs


# ── Formatting ────────────────────────────────────────────────────────────────


def format_overview(overview: AudienceOverview) -> str:
    """Форматировать обзор аудитории для Telegram."""
    lines = [
        f"👥 <b>Обзор аудитории</b>",
        f"",
        f"📊 Подписчики: <b>{overview.total_subscribers:,}</b>",
        f"🟢 Активные (7д): <b>{overview.active_users:,}</b> ({overview.active_rate}%)",
        f"⏱ Средняя сессия: <b>{overview.avg_session_duration_min} мин</b>",
        f"",
        f"📅 Пик активности: <b>{overview.peak_hour}:00 {overview.peak_day}</b>",
        f"",
        f"📈 Retention 7д: <b>{overview.retention_7d}%</b>",
        f"📈 Retention 30д: <b>{overview.retention_30d}%</b>",
    ]
    return "\n".join(lines)


def format_segments(segments: list[AudienceSegment]) -> str:
    """Форматировать сегменты аудитории для Telegram."""
    if not segments:
        return "📊 Нет данных для сегментации"

    lines = ["📊 <b>Сегменты аудитории</b>", ""]
    for seg in segments:
        bar = "█" * int(seg.percentage / 5) + "░" * (20 - int(seg.percentage / 5))
        lines.append(
            f"<b>{seg.name}</b>: {seg.user_count} ({seg.percentage}%)"
        )
        lines.append(f"  {bar}")
        lines.append(f"  _{seg.description}_")
        lines.append("")

    return "\n".join(lines)


def format_prediction(prediction: EngagementPrediction) -> str:
    """Форматировать прогноз вовлечённости для Telegram."""
    lines = [
        f"🔮 <b>Прогноз вовлечённости</b>",
        f"",
        f"📈 Прогнозируемый ER: <b>{prediction.predicted_er}%</b>",
        f"🎯 Уверенность: <b>{prediction.confidence * 100:.0f}%</b>",
        f"",
        f"⏰ Лучшее время: <b>{prediction.best_post_hour}:00 {prediction.best_post_day}</b>",
    ]

    if prediction.recommended_content_types:
        types_str = ", ".join(prediction.recommended_content_types)
        lines.append(f"📝 Рекомендуемые форматы: <b>{types_str}</b>")

    if prediction.factors:
        lines.append("")
        lines.append("<b>Факторы:</b>")
        for f in prediction.factors:
            lines.append(f"  • {f}")

    return "\n".join(lines)


def format_insights(insights: AudienceInsights) -> str:
    """Форматировать инсайты аудитории для Telegram."""
    trend_emoji = {
        "growing": "📈",
        "declining": "📉",
        "stable": "➡️",
    }.get(insights.growth_trend, "❓")

    lines = [
        f"💡 <b>Инсайты аудитории</b>",
        f"",
        f"{trend_emoji} Тренд: <b>{insights.growth_trend}</b> ({insights.growth_rate_pct}%)",
        f"⚠️ Риск оттока: <b>{insights.churn_risk_pct}%</b>",
        f"🏆 Качество аудитории: <b>{insights.audience_quality_score}/100</b>",
    ]

    if insights.top_activity_hours:
        hours_str = ", ".join(f"{h}:00" for h in insights.top_activity_hours)
        lines.append(f"⏰ Топ часы: <b>{hours_str}</b>")

    if insights.content_preferences:
        prefs_str = ", ".join(insights.content_preferences)
        lines.append(f"📝 Предпочтения: <b>{prefs_str}</b>")

    if insights.recommendations:
        lines.append("")
        lines.append("<b>Рекомендации:</b>")
        for rec in insights.recommendations:
            lines.append(f"  {rec}")

    return "\n".join(lines)
