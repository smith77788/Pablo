"""Infrastructure Pressure Score — composite metric 0-100 measuring infrastructure risk.

Pressure reflects how stressed the account infrastructure is right now:
  0-30   GREEN  — healthy, plenty of headroom
  31-60  YELLOW — moderate load, monitor closely
  61-80  ORANGE — high pressure, reduce operations
  81-100 RED    — critical, system may degrade

Factors (weights):
  - Cooldown ratio:   25% — % accounts in cooldown / max 50%+
  - Restriction ratio:15% — % restricted/banned/expired accounts
  - Flood density:    20% — avg flood events per account last 24h
  - Op queue depth:   20% — pending+running ops ratio vs capacity
  - Proxy failures:   10% — proxy error rate last 7d
  - Trust degradation:10% — % accounts with trust_score < 0.4
"""

from __future__ import annotations

import logging
import asyncpg

from database import db as _db

log = logging.getLogger(__name__)

_LEVEL_LABELS = {
    range(0, 31):  ("🟢", "Норма"),
    range(31, 61): ("🟡", "Умеренная"),
    range(61, 81): ("🟠", "Высокая"),
    range(81, 101):("🔴", "Критическая"),
}


def pressure_level(score: int) -> tuple[str, str]:
    for r, label in _LEVEL_LABELS.items():
        if score in r:
            return label
    return ("🔴", "Критическая")


async def compute_pressure(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Compute Infrastructure Pressure Score for owner. Returns dict with score + breakdown."""
    try:
        return await _compute(pool, owner_id)
    except Exception as e:
        log.warning("infra_pressure compute failed owner=%d: %s", owner_id, e)
        return {"score": 0, "level_emoji": "🟢", "level_label": "Норма", "breakdown": {}, "error": str(e)}


async def _compute(pool: asyncpg.Pool, owner_id: int) -> dict:
    # 1. Account stats — individual try/except so one failure doesn't break the score
    acc: dict = {}
    try:
        acc_rows = await pool.fetch(
            """SELECT
                   COUNT(*) FILTER (WHERE is_active) AS total_active,
                   COUNT(*) FILTER (WHERE is_active AND cooldown_until > NOW()) AS cooling,
                   COUNT(*) FILTER (WHERE COALESCE(acc_status,'active') NOT IN ('active','cooldown') AND is_active) AS restricted,
                   COUNT(*) FILTER (WHERE is_active AND COALESCE(trust_score,1.0) < 0.4) AS low_trust,
                   COALESCE(AVG(COALESCE(flood_count_7d,0)) FILTER (WHERE is_active), 0) AS avg_flood_7d
               FROM tg_accounts
               WHERE owner_id=$1""",
            owner_id,
        )
        acc = acc_rows[0] if acc_rows else {}
    except Exception as e:
        log.warning("infra_pressure: account stats query failed owner=%d: %s", owner_id, e)
    total = max(acc.get("total_active") or 1, 1)

    # 2. Op queue depth — isolated
    active_ops = 0
    try:
        queue_rows = await pool.fetch(
            "SELECT COUNT(*) AS cnt FROM operation_queue WHERE owner_id=$1 AND status IN ('pending','running')",
            owner_id,
        )
        active_ops = (queue_rows[0]["cnt"] if queue_rows else 0) or 0
    except Exception as e:
        log.warning("infra_pressure: queue depth query failed owner=%d: %s", owner_id, e)

    # 3. Proxy failures — isolated (table may not exist on older schemas)
    proxy_fail_rate = 0.0
    try:
        proxy_rows = await pool.fetch(
            """SELECT
                   COUNT(*) FILTER (WHERE success) AS ok,
                   COUNT(*) FILTER (WHERE NOT success) AS fail
               FROM proxy_quality_log pql
               JOIN user_proxies up ON up.id=pql.proxy_id
               WHERE up.owner_id=$1 AND pql.checked_at > NOW() - INTERVAL '7 days'""",
            owner_id,
        )
        proxy_total = ((proxy_rows[0]["ok"] or 0) + (proxy_rows[0]["fail"] or 0)) if proxy_rows else 0
        proxy_fail_rate = (proxy_rows[0]["fail"] or 0) / max(proxy_total, 1) if proxy_total > 0 else 0.0
    except Exception as e:
        log.warning("infra_pressure: proxy quality query failed owner=%d: %s", owner_id, e)

    # --- Compute component scores (0-100 each) ---

    # Cooldown ratio: 0 cool → 0, 50%+ cool → 100
    cool_ratio = (acc.get("cooling") or 0) / total
    c_cooldown = min(100, int(cool_ratio * 200))  # 50% → 100

    # Restriction ratio: any restricted → pressure
    restr_ratio = (acc.get("restricted") or 0) / total
    c_restriction = min(100, int(restr_ratio * 300))  # 33%+ → 100

    # Flood density: avg > 5 floods/7d → pressure
    avg_flood = float(acc.get("avg_flood_7d") or 0)
    c_flood = min(100, int(avg_flood / 5 * 100))

    # Queue depth: 0 ops → 0, 10+ ops → 100
    c_queue = min(100, int(active_ops / 10 * 100))

    # Proxy failures: 0% fail → 0, 50%+ fail → 100
    c_proxy = min(100, int(proxy_fail_rate * 200))

    # Trust degradation: 0% low trust → 0, 50%+ → 100
    trust_ratio = (acc.get("low_trust") or 0) / total
    c_trust = min(100, int(trust_ratio * 200))

    # Weighted sum
    score = int(
        c_cooldown    * 0.25 +
        c_restriction * 0.15 +
        c_flood       * 0.20 +
        c_queue       * 0.20 +
        c_proxy       * 0.10 +
        c_trust       * 0.10
    )
    score = max(0, min(100, score))

    breakdown = {
        "cooldown_accounts": int(acc.get("cooling") or 0),
        "restricted_accounts": int(acc.get("restricted") or 0),
        "low_trust_accounts": int(acc.get("low_trust") or 0),
        "avg_flood_7d": round(avg_flood, 1),
        "active_ops": int(active_ops),
        "proxy_fail_rate": round(proxy_fail_rate * 100, 1),
        "components": {
            "cooldown": c_cooldown,
            "restriction": c_restriction,
            "flood": c_flood,
            "queue": c_queue,
            "proxy": c_proxy,
            "trust": c_trust,
        },
        "total_accounts": total,
    }

    emoji, label = pressure_level(score)

    # Cache the result
    try:
        await _db.save_pressure_cache(pool, owner_id, score, breakdown)
    except Exception as e:
        log.warning("infra_pressure: cache save failed owner=%d: %s", owner_id, e)

    return {
        "score": score,
        "level_emoji": emoji,
        "level_label": label,
        "breakdown": breakdown,
    }


def format_pressure_report(data: dict) -> str:
    score = data.get("score", 0)
    emoji = data.get("level_emoji", "🟢")
    label = data.get("level_label", "Норма")
    bd = data.get("breakdown", {})
    comp = bd.get("components", {})

    bar_filled = round(score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Weights match _compute(): cooldown=25, restriction=15, flood=20, queue=20, proxy=10, trust=10
    _WEIGHTS = {
        "cooldown":    25,
        "restriction": 15,
        "flood":       20,
        "queue":       20,
        "proxy":       10,
        "trust":       10,
    }

    def _pts(key: str) -> tuple[int, int]:
        """Return (actual_points, max_points) for a component."""
        raw = comp.get(key, 0)  # 0-100 component score
        weight = _WEIGHTS[key]
        actual = round(raw * weight / 100)
        return actual, weight

    c_cool = _pts("cooldown")
    c_rest = _pts("restriction")
    c_fld  = _pts("flood")
    c_q    = _pts("queue")
    c_prx  = _pts("proxy")
    c_tr   = _pts("trust")

    lines = [
        "🌡 <b>Давление инфраструктуры</b>",
        "",
        f"{emoji} <b>{score}/100</b> — {label}",
        f"[{bar}]",
        "",
        "📊 <b>Breakdown по компонентам:</b>",
        f"  🕐 Кулдаун:        {c_cool[0]:2d}/{c_cool[1]} очков  ({bd.get('cooldown_accounts', 0)} акк)",
        f"  🚫 Ограничения:    {c_rest[0]:2d}/{c_rest[1]} очков  ({bd.get('restricted_accounts', 0)} акк)",
        f"  🌊 Флуд:           {c_fld[0]:2d}/{c_fld[1]} очков  (avg {bd.get('avg_flood_7d', 0)}/7д)",
        f"  ⚙️ Очередь:        {c_q[0]:2d}/{c_q[1]} очков  ({bd.get('active_ops', 0)} активных)",
        f"  🌐 Прокси-сбои:    {c_prx[0]:2d}/{c_prx[1]} очков  ({bd.get('proxy_fail_rate', 0.0)}%)",
        f"  ⚠️ Низкое доверие: {c_tr[0]:2d}/{c_tr[1]} очков  ({bd.get('low_trust_accounts', 0)} акк)",
    ]

    # Рекомендации по уровню давления
    if score >= 81:
        lines += [
            "",
            "⛔ <b>Критическое давление!</b>",
            "• Немедленно снизьте нагрузку на инфраструктуру",
            "• Часть аккаунтов требует восстановления",
            "• Добавьте аккаунты или снизьте нагрузку",
        ]
    elif score >= 71:
        lines += [
            "",
            "🔴 <b>Высокое давление!</b>",
            "• Добавьте аккаунты или снизьте нагрузку",
            "• Уменьшите количество одновременных операций",
            "• Рекомендуется авто-ротация кулдаун-аккаунтов",
        ]
    elif score >= 61:
        lines += [
            "",
            "⚠️ <b>Повышенное давление</b>",
            "• Уменьшите количество одновременных операций",
            "• Мониторьте flood-события",
        ]
    elif score >= 31:
        lines += ["", "💡 <b>Рекомендация:</b> инфраструктура под нагрузкой, следите за флудами."]
    else:
        lines += ["", "✅ <b>Инфраструктура в норме.</b> Продолжайте в том же духе."]

    return "\n".join(lines)
