"""Infrastructure Advisor — deterministic recommendations engine.

Analyzes account/proxy state and generates actionable recommendations.
No ML, no stochastic logic — pure rules-based analysis.
"""

from __future__ import annotations

import logging
import asyncpg

log = logging.getLogger(__name__)


async def get_recommendations(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Return list of recommendations sorted by severity (critical first)."""
    try:
        return await _analyze(pool, owner_id)
    except Exception as e:
        log.warning("infra_advisor failed owner=%d: %s", owner_id, e)
        return []


async def _analyze(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    recs: list[dict] = []

    # 1. Accounts in cooldown for a long time (> 6h)
    cooling_long = await pool.fetch(
        """SELECT id, phone, first_name, EXTRACT(EPOCH FROM (cooldown_until - NOW()))/3600 AS hours_left
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE
             AND cooldown_until > NOW() + INTERVAL '6 hours'
           ORDER BY hours_left DESC LIMIT 5""",
        owner_id,
    )
    if cooling_long:
        names = ", ".join(
            (r.get("first_name") or r.get("phone") or f"id{r['id']}")
            for r in cooling_long[:3]
        )
        recs.append({
            "severity": "warning",
            "icon": "⏳",
            "title": f"Долгий кулдаун: {len(cooling_long)} аккаунт(ов)",
            "text": f"Аккаунты {names} и другие заблокированы флудом на >6 часов. Снизьте интенсивность операций.",
            "action": "accounts",
        })

    # 2. Accounts with many flood events (last 7d > 10)
    flood_heavy = await pool.fetch(
        """SELECT id, phone, first_name, flood_count_7d
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE AND COALESCE(flood_count_7d,0) > 10
           ORDER BY flood_count_7d DESC LIMIT 5""",
        owner_id,
    )
    if flood_heavy:
        worst = flood_heavy[0]
        name = worst.get("first_name") or worst.get("phone") or f"id{worst['id']}"
        recs.append({
            "severity": "warning",
            "icon": "🌊",
            "title": f"Высокая флуд-активность: {len(flood_heavy)} аккаунт(ов)",
            "text": f"Аккаунт {name} получил {worst['flood_count_7d']} флудов за 7 дней. Аккаунт перегружен — дайте ему отдохнуть.",
            "action": "accounts",
        })

    # 3. Low trust accounts still in rotation
    low_trust = await pool.fetch(
        """SELECT id, phone, first_name, trust_score
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE AND COALESCE(trust_score,1.0) < 0.3
           ORDER BY trust_score ASC LIMIT 5""",
        owner_id,
    )
    if low_trust:
        names = ", ".join(
            (r.get("first_name") or r.get("phone") or f"id{r['id']}")
            for r in low_trust[:3]
        )
        recs.append({
            "severity": "critical",
            "icon": "🚨",
            "title": f"Низкое доверие: {len(low_trust)} аккаунт(ов)",
            "text": f"Аккаунты {names} имеют trust_score < 0.3. Используйте их для некритичных операций или разогрейте.",
            "action": "warmup",
        })

    # 4. Restricted/banned accounts not cleaned up
    restricted = await pool.fetch(
        """SELECT id, phone, first_name, acc_status
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE
             AND COALESCE(acc_status,'active') IN ('spamblock','banned','deactivated','session_expired')
           ORDER BY added_at LIMIT 10""",
        owner_id,
    )
    if restricted:
        recs.append({
            "severity": "critical",
            "icon": "🚫",
            "title": f"Проблемные аккаунты: {len(restricted)} шт",
            "text": f"Есть аккаунты со статусом spamblock/banned/deactivated, которые всё ещё числятся активными. Очистите их.",
            "action": "cleaner",
        })

    # 5. All accounts in one pool / no pool diversity
    pool_stats = await pool.fetch(
        "SELECT pool, COUNT(*) AS cnt FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE GROUP BY pool",
        owner_id,
    )
    null_pool_count = sum(r["cnt"] for r in pool_stats if r["pool"] is None)
    total_acc = sum(r["cnt"] for r in pool_stats)
    if total_acc >= 3 and null_pool_count == total_acc:
        recs.append({
            "severity": "info",
            "icon": "🏊",
            "title": "Аккаунты не распределены по пулам",
            "text": "Назначьте аккаунтам пулы (strike, warmup, publish и т.д.) для умного распределения нагрузки.",
            "action": "accounts",
        })

    # 6. Proxy failure rate high
    proxy_stats = await pool.fetch(
        """SELECT up.label, up.id,
               COUNT(q.id) FILTER (WHERE NOT q.success) AS fails,
               COUNT(q.id) AS total
           FROM user_proxies up
           LEFT JOIN proxy_quality_log q ON q.proxy_id=up.id AND q.checked_at > NOW()-INTERVAL '7 days'
           WHERE up.owner_id=$1
           GROUP BY up.id, up.label
           HAVING COUNT(q.id) > 5 AND COUNT(q.id) FILTER (WHERE NOT q.success)::float / COUNT(q.id) > 0.3""",
        owner_id,
    )
    if proxy_stats:
        names = ", ".join(r["label"] or f"proxy#{r['id']}" for r in proxy_stats[:3])
        recs.append({
            "severity": "warning",
            "icon": "🌐",
            "title": f"Нестабильные прокси: {len(proxy_stats)} шт",
            "text": f"Прокси {names} имеют >30% ошибок за последние 7 дней. Замените или проверьте их.",
            "action": "proxies",
        })

    # 7. Operation queue stale (stuck running for > 2h)
    stale_ops = await pool.fetch(
        """SELECT COUNT(*) AS cnt FROM operation_queue
           WHERE owner_id=$1 AND status='running'
             AND COALESCE(updated_at, created_at) < NOW() - INTERVAL '2 hours'""",
        owner_id,
    )
    stale_cnt = (stale_ops[0]["cnt"] if stale_ops else 0) or 0
    if stale_cnt > 0:
        recs.append({
            "severity": "warning",
            "icon": "🔄",
            "title": f"Зависшие операции: {stale_cnt} шт",
            "text": "Есть операции в статусе 'running' более 2 часов. Возможно, они зависли — проверьте очередь.",
            "action": "tasks",
        })

    # 8. No active accounts at all
    active_count = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", owner_id
    )
    if (active_count or 0) == 0:
        recs.append({
            "severity": "critical",
            "icon": "📱",
            "title": "Нет активных аккаунтов",
            "text": "Добавьте хотя бы один аккаунт Telegram для использования операционных функций.",
            "action": "accounts",
        })

    # Sort: critical first, then warning, then info
    order = {"critical": 0, "warning": 1, "info": 2}
    recs.sort(key=lambda r: order.get(r.get("severity", "info"), 2))
    return recs


def format_recommendations(recs: list[dict]) -> str:
    if not recs:
        return "✅ <b>Рекомендаций нет</b> — инфраструктура в норме."
    lines = ["🎯 <b>Рекомендации инфраструктуры</b>\n"]
    for r in recs:
        icon = r.get("icon", "•")
        title = r.get("title", "")
        text = r.get("text", "")
        lines.append(f"{icon} <b>{title}</b>\n   {text}\n")
    return "\n".join(lines).strip()
