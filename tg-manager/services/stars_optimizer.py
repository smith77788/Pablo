"""Telegram Stars Yield Optimizer — A/B testing and conversion optimization for Stars monetization."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class StarsExperiment:
    id: int
    bot_id: int
    owner_id: int
    name: str
    content_type: str
    price_a: int
    price_b: int
    conversions_a: int = 0
    conversions_b: int = 0
    impressions_a: int = 0
    impressions_b: int = 0
    revenue_a: int = 0
    revenue_b: int = 0
    status: str = "active"
    winner: Optional[str] = None
    significance: Optional[float] = None


# ── DB helpers ────────────────────────────────────────────────────────────────

async def create_experiment(
    pool: asyncpg.Pool,
    bot_id: int,
    owner_id: int,
    name: str,
    content_type: str,
    price_a: int,
    price_b: int,
) -> StarsExperiment:
    """Create a new A/B experiment and return the StarsExperiment object."""
    row = await pool.fetchrow(
        """
        INSERT INTO stars_experiments
            (bot_id, owner_id, name, content_type, price_a, price_b)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        bot_id, owner_id, name, content_type, price_a, price_b,
    )
    return _row_to_exp(row)


async def record_impression(
    pool: asyncpg.Pool,
    experiment_id: int,
    variant: str,
    user_id: int,
) -> None:
    """Record that user_id was shown variant (a/b) of the experiment."""
    col = "impressions_a" if variant == "a" else "impressions_b"
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE stars_experiments SET {col} = {col} + 1 WHERE id = $1",
            experiment_id,
        )
        await conn.execute(
            """
            INSERT INTO stars_events
                (experiment_id, bot_id, user_id, variant, event_type, stars_amount)
            SELECT $1, bot_id, $2, $3, 'impression', 0
            FROM stars_experiments WHERE id = $1
            """,
            experiment_id, user_id, variant,
        )


async def record_conversion(
    pool: asyncpg.Pool,
    experiment_id: int,
    variant: str,
    user_id: int,
    stars_amount: int,
) -> None:
    """Record a purchase (conversion) for variant a or b."""
    conv_col = "conversions_a" if variant == "a" else "conversions_b"
    rev_col = "revenue_a" if variant == "a" else "revenue_b"
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE stars_experiments SET {conv_col} = {conv_col} + 1, "
            f"{rev_col} = {rev_col} + $2 WHERE id = $1",
            experiment_id, stars_amount,
        )
        await conn.execute(
            """
            INSERT INTO stars_events
                (experiment_id, bot_id, user_id, variant, event_type, stars_amount)
            SELECT $1, bot_id, $2, $3, 'conversion', $4
            FROM stars_experiments WHERE id = $1
            """,
            experiment_id, user_id, variant, stars_amount,
        )


# ── Statistical evaluation ───────────────────────────────────────────────────

def _chi_square_p(a_conv: int, a_imp: int, b_conv: int, b_imp: int) -> float:
    """
    Two-proportion chi-square test.
    Returns p-value approximation using normal approximation.
    Lower p → more significant difference.
    Returns 1.0 if data is insufficient.
    """
    if a_imp < 30 or b_imp < 30:
        return 1.0
    n = a_imp + b_imp
    p_pool = (a_conv + b_conv) / n
    if p_pool == 0 or p_pool == 1:
        return 1.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / a_imp + 1 / b_imp))
    if se == 0:
        return 1.0
    p_a = a_conv / a_imp
    p_b = b_conv / b_imp
    z = abs(p_a - p_b) / se
    # approximate two-tailed p-value from z-score
    p_val = 2 * (1 - _norm_cdf(z))
    return max(0.0, min(1.0, p_val))


def _norm_cdf(z: float) -> float:
    """Approximation of the standard normal CDF."""
    # Abramowitz & Stegun approximation 7.1.26
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    result = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
    return result if z >= 0 else 1.0 - result


async def evaluate_experiment(pool: asyncpg.Pool, experiment_id: int) -> dict:
    """
    Compute conversion rates and statistical significance for the experiment.
    Returns dict with: cr_a, cr_b, p_value, winner (or None), updated.
    Declares winner if p < 0.05 and each arm has >= 100 impressions.
    """
    row = await pool.fetchrow(
        "SELECT * FROM stars_experiments WHERE id = $1", experiment_id
    )
    if not row:
        return {"error": "not found"}

    imp_a = row["impressions_a"]
    imp_b = row["impressions_b"]
    conv_a = row["conversions_a"]
    conv_b = row["conversions_b"]

    cr_a = conv_a / imp_a if imp_a else 0.0
    cr_b = conv_b / imp_b if imp_b else 0.0
    p_val = _chi_square_p(conv_a, imp_a, conv_b, imp_b)

    winner = None
    completed = False

    if imp_a >= 100 and imp_b >= 100 and p_val < 0.05:
        winner = "a" if cr_a >= cr_b else "b"
        completed = True

    # Update DB
    if completed:
        await pool.execute(
            """
            UPDATE stars_experiments
            SET significance = $2, winner = $3, status = 'completed', completed_at = NOW()
            WHERE id = $1
            """,
            experiment_id, p_val, winner,
        )
    else:
        await pool.execute(
            "UPDATE stars_experiments SET significance = $2 WHERE id = $1",
            experiment_id, p_val,
        )

    return {
        "experiment_id": experiment_id,
        "cr_a": cr_a,
        "cr_b": cr_b,
        "imp_a": imp_a,
        "imp_b": imp_b,
        "conv_a": conv_a,
        "conv_b": conv_b,
        "p_value": p_val,
        "winner": winner,
        "completed": completed,
    }


# ── Recommendations ───────────────────────────────────────────────────────────

async def get_recommendations(
    pool: asyncpg.Pool, bot_id: int, owner_id: int
) -> list[str]:
    """
    Returns a list of human-readable Russian recommendations based on
    completed experiments for this bot.
    """
    rows = await pool.fetch(
        """
        SELECT * FROM stars_experiments
        WHERE bot_id = $1 AND owner_id = $2 AND status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 20
        """,
        bot_id, owner_id,
    )
    recs: list[str] = []
    for row in rows:
        winner = row["winner"]
        if not winner:
            continue
        if winner == "a":
            win_price = row["price_a"]
            lose_price = row["price_b"]
            win_conv = row["conversions_a"]
            win_imp = row["impressions_a"]
            lose_conv = row["conversions_b"]
            lose_imp = row["impressions_b"]
        else:
            win_price = row["price_b"]
            lose_price = row["price_a"]
            win_conv = row["conversions_b"]
            win_imp = row["impressions_b"]
            lose_conv = row["conversions_a"]
            lose_imp = row["impressions_a"]

        cr_win = win_conv / win_imp if win_imp else 0
        cr_lose = lose_conv / lose_imp if lose_imp else 0

        if cr_lose > 0:
            uplift_pct = round((cr_win - cr_lose) / cr_lose * 100)
        else:
            uplift_pct = 0

        direction = "Снизьте" if win_price < lose_price else "Повысьте"
        recs.append(
            f"💡 <b>{row['name']}</b>: {direction} цену с "
            f"<b>{lose_price}</b> до <b>{win_price} Stars</b> — "
            f"конверсия вырастет на ~{uplift_pct}% "
            f"(вариант {'A' if winner == 'a' else 'B'} победил)"
        )

    if not recs:
        recs.append(
            "ℹ️ Пока нет завершённых экспериментов. "
            "Запустите A/B тест, чтобы получить рекомендации по ценообразованию."
        )
    return recs


# ── Background loop ───────────────────────────────────────────────────────────

async def run(pool: asyncpg.Pool, bot) -> None:
    """
    Background service: evaluates all active experiments every 6 hours,
    sends notification to owner when an experiment completes.
    """
    log.info("[StarsOptimizer] background loop started")
    while True:
        try:
            active = await pool.fetch(
                "SELECT id, owner_id, name FROM stars_experiments WHERE status = 'active'"
            )
            for row in active:
                try:
                    result = await evaluate_experiment(pool, row["id"])
                    if result.get("completed") and result.get("winner"):
                        winner_letter = result["winner"].upper()
                        cr_a = round(result["cr_a"] * 100, 1)
                        cr_b = round(result["cr_b"] * 100, 1)
                        text = (
                            f"🏆 <b>A/B эксперимент завершён!</b>\n\n"
                            f"Название: <b>{row['name']}</b>\n"
                            f"Победитель: вариант <b>{winner_letter}</b>\n"
                            f"CR-A: {cr_a}% | CR-B: {cr_b}%\n\n"
                            f"Откройте <b>Stars Hub</b> для рекомендаций."
                        )
                        try:
                            await bot.send_message(
                                row["owner_id"], text, parse_mode="HTML"
                            )
                        except Exception as notify_err:
                            log.warning(
                                "[StarsOptimizer] notify owner %s failed: %s",
                                row["owner_id"], notify_err,
                            )
                except Exception as exp_err:
                    log.warning(
                        "[StarsOptimizer] evaluate exp %s failed: %s",
                        row["id"], exp_err,
                    )
        except asyncio.CancelledError:
            log.info("[StarsOptimizer] background loop cancelled")
            raise
        except Exception as err:
            log.error("[StarsOptimizer] loop error: %s", err)
        await asyncio.sleep(6 * 3600)


# ── Internal helper ───────────────────────────────────────────────────────────

def _row_to_exp(row: asyncpg.Record) -> StarsExperiment:
    return StarsExperiment(
        id=row["id"],
        bot_id=row["bot_id"],
        owner_id=row["owner_id"],
        name=row["name"],
        content_type=row["content_type"],
        price_a=row["price_a"],
        price_b=row["price_b"],
        conversions_a=row["conversions_a"],
        conversions_b=row["conversions_b"],
        impressions_a=row["impressions_a"],
        impressions_b=row["impressions_b"],
        revenue_a=row["revenue_a"],
        revenue_b=row["revenue_b"],
        status=row["status"],
        winner=row["winner"],
        significance=row["significance"],
    )
