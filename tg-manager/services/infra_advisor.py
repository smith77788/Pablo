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

    # 9. Accounts with poor memory performance (persistent failures from infra_memory_accounts)
    try:
        poor_memory = await pool.fetch(
            """SELECT ima.account_id,
                      COALESCE(a.first_name, a.phone, 'id'||ima.account_id::text) AS label,
                      ima.successes, ima.failures,
                      (ima.successes::float / NULLIF(ima.successes + ima.failures, 0)) AS success_rate
               FROM infra_memory_accounts ima
               JOIN tg_accounts a ON a.id = ima.account_id
               WHERE a.owner_id=$1 AND a.is_active=TRUE
                 AND (ima.successes + ima.failures) >= 20
                 AND (ima.successes::float / (ima.successes + ima.failures)) < 0.30
               ORDER BY success_rate ASC LIMIT 5""",
            owner_id,
        )
        if poor_memory:
            names = ", ".join(r["label"] for r in poor_memory[:3])
            worst_rate = int((poor_memory[0]["success_rate"] or 0) * 100)
            recs.append({
                "severity": "warning",
                "icon": "📉",
                "title": f"Хроническая низкая эффективность: {len(poor_memory)} акк",
                "text": (
                    f"Аккаунты {names} успешно выполняют <{worst_rate}% операций на основе "
                    f"исторических данных. Рекомендуется разогрев или исключение из активных операций."
                ),
                "action": "warmup",
            })
    except Exception:
        pass

    # 10. Pool concentration — one named pool has >80% of all accounts (uneven distribution)
    try:
        if total_acc >= 4:
            named_pools = [(r["pool"], r["cnt"]) for r in pool_stats if r["pool"] is not None]
            for pool_name, cnt in named_pools:
                if cnt / total_acc > 0.80:
                    recs.append({
                        "severity": "info",
                        "icon": "⚖️",
                        "title": f"Дисбаланс пулов: {pool_name!r} перегружен",
                        "text": (
                            f"{cnt} из {total_acc} активных аккаунтов в пуле «{pool_name}». "
                            f"Распределите аккаунты по нескольким пулам для снижения точки отказа."
                        ),
                        "action": "accounts",
                    })
                    break
    except Exception:
        pass

    # 11. Recent operation failure spike (last 30 ops, >45% failed/error)
    try:
        recent_ops = await pool.fetch(
            """SELECT status, COUNT(*) AS cnt
               FROM operation_queue
               WHERE owner_id=$1 AND created_at > NOW() - INTERVAL '48 hours'
               GROUP BY status""",
            owner_id,
        )
        op_totals = {r["status"]: int(r["cnt"]) for r in recent_ops}
        total_recent = sum(op_totals.values())
        failed_recent = op_totals.get("failed", 0) + op_totals.get("error", 0)
        if total_recent >= 10 and failed_recent / total_recent > 0.45:
            fail_pct = int(failed_recent / total_recent * 100)
            recs.append({
                "severity": "warning",
                "icon": "📛",
                "title": f"Всплеск ошибок операций: {fail_pct}% за 48ч",
                "text": (
                    f"{failed_recent} из {total_recent} последних операций завершились неудачей. "
                    f"Возможна перегрузка аккаунтов или проблемы с прокси."
                ),
                "action": "tasks",
            })
    except Exception:
        pass

    # 12. Accounts without proxy when user has proxies configured
    try:
        user_proxy_count = await pool.fetchval(
            "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
            owner_id,
        )
        if (user_proxy_count or 0) > 0:
            no_proxy_accs = await pool.fetchval(
                """SELECT COUNT(*) FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND proxy_id IS NULL""",
                owner_id,
            )
            if (no_proxy_accs or 0) > 0:
                recs.append({
                    "severity": "info",
                    "icon": "🔌",
                    "title": f"Аккаунты без прокси: {no_proxy_accs} шт",
                    "text": (
                        f"У вас настроены прокси, но {no_proxy_accs} аккаунт(ов) работают без них. "
                        f"Назначьте прокси для защиты реального IP."
                    ),
                    "action": "proxies",
                })
    except Exception:
        pass

    # 13. High-trust idle accounts (trust>0.7 but not used >7 days → underutilized asset)
    try:
        idle_high_trust = await pool.fetch(
            """SELECT id, COALESCE(first_name, phone, 'id'||id::text) AS label,
                      trust_score, last_used
               FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND COALESCE(trust_score, 0) > 0.70
                 AND (last_used IS NULL OR last_used < NOW() - INTERVAL '7 days')
                 AND (cooldown_until IS NULL OR cooldown_until < NOW())
               ORDER BY trust_score DESC LIMIT 5""",
            owner_id,
        )
        if idle_high_trust:
            names = ", ".join(r["label"] for r in idle_high_trust[:3])
            recs.append({
                "severity": "info",
                "icon": "💤",
                "title": f"Неиспользуемые надёжные аккаунты: {len(idle_high_trust)} шт",
                "text": (
                    f"Аккаунты {names} имеют высокий trust_score, но не использовались >7 дней. "
                    f"Включите их в операции для максимальной эффективности."
                ),
                "action": "accounts",
            })
    except Exception:
        pass

    # Sort: critical first, then warning, then info
    order = {"critical": 0, "warning": 1, "info": 2}
    recs.sort(key=lambda r: order.get(r.get("severity", "info"), 2))
    return recs


def format_recommendations(recs: list[dict]) -> str:
    import html as _html
    if not recs:
        return "✅ <b>Рекомендаций нет</b> — инфраструктура в норме."
    lines = ["🎯 <b>Рекомендации инфраструктуры</b>\n"]
    for r in recs:
        icon = r.get("icon", "•")
        title = _html.escape(r.get("title", ""))
        text = _html.escape(r.get("text", ""))
        lines.append(f"{icon} <b>{title}</b>\n   {text}\n")
    return "\n".join(lines).strip()
