"""Audience DNA — deep behavioral profiling and content optimization recommendations."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_LOOP_INTERVAL = 86_400  # 24 hours


@dataclass
class AudienceDNA:
    bot_id: int
    owner_id: int
    peak_hours: list[int] = field(default_factory=list)
    peak_days: list[str] = field(default_factory=list)
    best_content_types: list[str] = field(default_factory=list)
    avg_engagement_rate: float = 0.0
    churn_risk_pct: float = 0.0
    top_topics: list[str] = field(default_factory=list)
    total_users_analyzed: int = 0
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── DB helpers ────────────────────────────────────────────────────────────────


async def get_dna(pool: asyncpg.Pool, bot_id: int) -> AudienceDNA | None:
    """Return most recently computed DNA for bot, or None."""
    row = await pool.fetchrow(
        """
        SELECT * FROM audience_dna
        WHERE bot_id = $1
        ORDER BY computed_at DESC
        LIMIT 1
        """,
        bot_id,
    )
    if not row:
        return None
    return AudienceDNA(
        bot_id=row["bot_id"],
        owner_id=row["owner_id"],
        peak_hours=list(row["peak_hours"] or []),
        peak_days=list(row["peak_days"] or []),
        best_content_types=list(row["best_content_types"] or []),
        avg_engagement_rate=float(row["avg_engagement_rate"] or 0),
        churn_risk_pct=float(row["churn_risk_pct"] or 0),
        top_topics=list(row["top_topics"] or []),
        total_users_analyzed=int(row["total_users_analyzed"] or 0),
        computed_at=row["computed_at"],
    )


async def track_content_performance(
    pool: asyncpg.Pool,
    bot_id: int,
    message_id: int | None,
    content_type: str,
    views: int,
    reactions: int,
    forwards: int,
    publish_hour: int,
    publish_weekday: int,
    replies: int = 0,
) -> None:
    """Record performance metrics for a published post."""
    await pool.execute(
        """
        INSERT INTO content_performance
            (bot_id, message_id, content_type, views, reactions, forwards, replies,
             publish_hour, publish_weekday)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        bot_id,
        message_id,
        content_type,
        max(0, views),
        max(0, reactions),
        max(0, forwards),
        max(0, replies),
        publish_hour % 24,
        publish_weekday % 7,
    )


# ── Core computation ──────────────────────────────────────────────────────────


async def compute_dna(
    pool: asyncpg.Pool,
    bot_id: int,
    owner_id: int,
) -> AudienceDNA:
    """Analyse behavioral data and return (+ persist) AudienceDNA."""

    # ── 1. Peak hours / days from bot_user_memory or user_activity ───────────
    peak_hours: list[int] = []
    peak_days: list[str] = []
    total_users = 0
    churn_risk_pct = 0.0

    # Try activity heatmap from user_activity (existing table)
    heatmap_rows: list[asyncpg.Record] = []
    try:
        heatmap_rows = await pool.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM last_active)::INT AS hour,
                EXTRACT(DOW FROM last_active)::INT  AS dow,
                COUNT(*) AS cnt
            FROM user_activity
            WHERE bot_id = $1
              AND last_active IS NOT NULL
            GROUP BY 1, 2
            ORDER BY cnt DESC
            """,
            bot_id,
        )
    except Exception:
        pass

    if heatmap_rows:
        hour_counts: Counter[int] = Counter()
        dow_counts: Counter[int] = Counter()
        for r in heatmap_rows:
            hour_counts[r["hour"]] += r["cnt"]
            dow_counts[r["dow"]] += r["cnt"]

        # Top-3 hours
        peak_hours = [h for h, _ in hour_counts.most_common(3)]
        # Top-2 weekdays (0=Sun in PostgreSQL DOW, map to Mon-based)
        pg_dow_to_mon = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
        peak_days = [
            _DAY_NAMES[pg_dow_to_mon.get(d, 0)]
            for d, _ in dow_counts.most_common(2)
        ]

    # ── 2. Churn risk from user_activity (inactive 14+ days) ─────────────────
    try:
        stats = await pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE last_active < NOW() - INTERVAL '14 days'
                )::FLOAT AS inactive_cnt
            FROM user_activity
            WHERE bot_id = $1
            """,
            bot_id,
        )
        if stats and stats["total"]:
            total_users = int(stats["total"])
            churn_risk_pct = round(stats["inactive_cnt"] / stats["total"] * 100, 1)
    except Exception:
        pass

    # Fallback: count from bot_users / audience table
    if total_users == 0:
        try:
            row = await pool.fetchrow(
                "SELECT COUNT(*) AS cnt FROM bot_users WHERE bot_id=$1", bot_id
            )
            if row:
                total_users = int(row["cnt"])
        except Exception:
            pass

    # ── 3. Best content types from content_performance ────────────────────────
    best_content_types: list[str] = []
    avg_engagement_rate = 0.0
    try:
        cp_rows = await pool.fetch(
            """
            SELECT content_type,
                   AVG(engagement_rate) AS avg_er,
                   COUNT(*) AS cnt
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
        if cp_rows:
            best_content_types = [r["content_type"] for r in cp_rows if r["content_type"]]
            all_ers = [float(r["avg_er"]) for r in cp_rows]
            avg_engagement_rate = round(sum(all_ers) / len(all_ers) * 100, 2) if all_ers else 0.0

        # Also compute peak hours from content_performance (when posts perform best)
        if not peak_hours:
            hour_perf = await pool.fetch(
                """
                SELECT publish_hour,
                       AVG(engagement_rate) AS avg_er,
                       COUNT(*) AS cnt
                FROM content_performance
                WHERE bot_id = $1 AND views > 0
                GROUP BY publish_hour
                HAVING COUNT(*) >= 1
                ORDER BY avg_er DESC
                LIMIT 3
                """,
                bot_id,
            )
            peak_hours = [r["publish_hour"] for r in hour_perf if r["publish_hour"] is not None]

            dow_perf = await pool.fetch(
                """
                SELECT publish_weekday,
                       AVG(engagement_rate) AS avg_er
                FROM content_performance
                WHERE bot_id = $1 AND views > 0
                GROUP BY publish_weekday
                ORDER BY avg_er DESC
                LIMIT 2
                """,
                bot_id,
            )
            peak_days = [
                _DAY_NAMES[r["publish_weekday"]]
                for r in dow_perf
                if r["publish_weekday"] is not None
            ]
    except Exception as exc:
        log.warning("compute_dna: content_performance query failed: %s", exc)

    # ── 4. Top topics from bot_user_memory (if present) ───────────────────────
    top_topics: list[str] = []
    try:
        mem_rows = await pool.fetch(
            """
            SELECT fact_value
            FROM bot_user_facts
            WHERE bot_id = $1
              AND fact_key = 'interests'
            LIMIT 500
            """,
            bot_id,
        )
        topic_counter: Counter[str] = Counter()
        for r in mem_rows:
            raw = r["fact_value"]
            if isinstance(raw, str):
                for t in raw.split(","):
                    t = t.strip().lower()
                    if t:
                        topic_counter[t] += 1
        top_topics = [t for t, _ in topic_counter.most_common(5)]
    except Exception:
        pass

    # ── 5. Persist ────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    dna = AudienceDNA(
        bot_id=bot_id,
        owner_id=owner_id,
        peak_hours=peak_hours,
        peak_days=peak_days,
        best_content_types=best_content_types,
        avg_engagement_rate=avg_engagement_rate,
        churn_risk_pct=churn_risk_pct,
        top_topics=top_topics,
        total_users_analyzed=total_users,
        computed_at=now,
    )

    try:
        await pool.execute(
            """
            INSERT INTO audience_dna
                (bot_id, owner_id, peak_hours, peak_days, best_content_types,
                 avg_engagement_rate, churn_risk_pct, top_topics,
                 total_users_analyzed, computed_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            bot_id,
            owner_id,
            dna.peak_hours,
            dna.peak_days,
            dna.best_content_types,
            dna.avg_engagement_rate,
            dna.churn_risk_pct,
            dna.top_topics,
            dna.total_users_analyzed,
            now,
        )
    except Exception as exc:
        log.error("compute_dna: failed to persist: %s", exc)

    log.info(
        "audience_dna computed bot_id=%s users=%s churn=%.1f%%",
        bot_id, total_users, churn_risk_pct,
    )
    return dna


# ── Recommendations ───────────────────────────────────────────────────────────

_CONTENT_TYPE_LABELS: dict[str, str] = {
    "text": "текстовые посты",
    "photo": "фото",
    "video": "видео",
    "poll": "опросы",
    "link": "ссылки",
    "personal": "личные истории",
    "expert": "экспертный контент",
    "promo": "промо",
}


def generate_recommendations(
    dna: AudienceDNA,
    ai_provider: Any = None,
) -> list[str]:
    """Return list of human-readable Russian recommendations based on DNA."""
    recs: list[str] = []

    # Peak time
    if dna.peak_hours:
        hours_str = ", ".join(f"{h}:00" for h in sorted(dna.peak_hours))
        days_str = " и ".join(dna.peak_days) if dna.peak_days else "рабочие дни"
        recs.append(
            f"📅 <b>Лучшее время публикации:</b> {hours_str} UTC в {days_str}"
        )
    else:
        recs.append(
            "📅 <b>Время публикации:</b> недостаточно данных — начните публиковать контент и отслеживайте вовлечённость"
        )

    # Content types
    if dna.best_content_types:
        labels = [
            _CONTENT_TYPE_LABELS.get(ct, ct) for ct in dna.best_content_types[:3]
        ]
        recs.append(
            f"📝 <b>Топ-форматы контента:</b> {', '.join(labels)}"
        )
    else:
        recs.append(
            "📝 <b>Форматы контента:</b> добавьте метрики постов через /track_post для анализа"
        )

    # Engagement rate
    if dna.avg_engagement_rate > 0:
        quality = (
            "отличный" if dna.avg_engagement_rate >= 5
            else "хороший" if dna.avg_engagement_rate >= 2
            else "низкий"
        )
        recs.append(
            f"📈 <b>Средняя вовлечённость:</b> {dna.avg_engagement_rate:.1f}% — {quality} показатель"
        )

    # Churn risk
    if dna.churn_risk_pct > 0:
        risk_emoji = "🔴" if dna.churn_risk_pct > 40 else "🟡" if dna.churn_risk_pct > 20 else "🟢"
        recs.append(
            f"⚠️ <b>Риск оттока:</b> {dna.churn_risk_pct:.0f}% аудитории неактивны 14+ дней {risk_emoji}"
        )
        if dna.churn_risk_pct > 30:
            recs.append(
                "💡 <b>Рекомендация:</b> запустите реактивационную рассылку для неактивных пользователей"
            )

    # Top topics
    if dna.top_topics:
        topics_str = ", ".join(dna.top_topics[:5])
        recs.append(
            f"🏷️ <b>Популярные темы:</b> {topics_str}"
        )

    # Audience size context
    if dna.total_users_analyzed > 0:
        recs.append(
            f"👥 <b>Проанализировано пользователей:</b> {dna.total_users_analyzed:,}"
        )

    # Actionable tip based on peak hours
    if dna.peak_hours and dna.best_content_types:
        top_ct = _CONTENT_TYPE_LABELS.get(dna.best_content_types[0], dna.best_content_types[0])
        top_hour = dna.peak_hours[0]
        top_day = dna.peak_days[0] if dna.peak_days else "любой день"
        recs.append(
            f"🎯 <b>Главный совет:</b> публикуйте {top_ct} в {top_hour}:00 UTC по {top_day} — "
            f"максимальный охват вашей аудитории"
        )

    return recs


# ── History helpers ────────────────────────────────────────────────────────────


async def get_dna_history(
    pool: asyncpg.Pool,
    bot_id: int,
    limit: int = 5,
) -> list[AudienceDNA]:
    """Return last N DNA snapshots for a bot."""
    rows = await pool.fetch(
        """
        SELECT * FROM audience_dna
        WHERE bot_id = $1
        ORDER BY computed_at DESC
        LIMIT $2
        """,
        bot_id,
        limit,
    )
    result = []
    for row in rows:
        result.append(
            AudienceDNA(
                bot_id=row["bot_id"],
                owner_id=row["owner_id"],
                peak_hours=list(row["peak_hours"] or []),
                peak_days=list(row["peak_days"] or []),
                best_content_types=list(row["best_content_types"] or []),
                avg_engagement_rate=float(row["avg_engagement_rate"] or 0),
                churn_risk_pct=float(row["churn_risk_pct"] or 0),
                top_topics=list(row["top_topics"] or []),
                total_users_analyzed=int(row["total_users_analyzed"] or 0),
                computed_at=row["computed_at"],
            )
        )
    return result


def _delta_str(current: float, previous: float, unit: str = "") -> str:
    """Format delta between two values with arrow."""
    delta = current - previous
    if abs(delta) < 0.01:
        return "без изменений"
    arrow = "↑" if delta > 0 else "↓"
    return f"{arrow} {abs(delta):.1f}{unit}"


# ── Background loop ────────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Any) -> None:
    """Background task: recompute DNA for all active bots every 24 hours."""
    log.info("audience_dna: background loop started")
    while True:
        try:
            await _recompute_all(pool)
        except Exception as exc:
            log.error("audience_dna: recompute loop error: %s", exc)
        await asyncio.sleep(_LOOP_INTERVAL)


async def _recompute_all(pool: asyncpg.Pool) -> None:
    """Compute DNA for every active managed bot."""
    try:
        bots = await pool.fetch(
            "SELECT bot_id, owner_id FROM managed_bots WHERE is_active = TRUE"
        )
    except Exception as exc:
        log.error("audience_dna: failed to fetch active bots: %s", exc)
        return

    log.info("audience_dna: recomputing for %d bots", len(bots))
    for b in bots:
        try:
            await compute_dna(pool, b["bot_id"], b["owner_id"])
            await asyncio.sleep(0.5)  # avoid DB overload
        except Exception as exc:
            log.warning("audience_dna: failed for bot_id=%s: %s", b["bot_id"], exc)
