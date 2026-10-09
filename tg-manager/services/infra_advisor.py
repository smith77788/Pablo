"""Infrastructure Advisor — deterministic recommendations engine.

Analyzes account/proxy state and generates actionable recommendations.
No ML, no stochastic logic — pure rules-based analysis.
"""

from __future__ import annotations

import logging
import asyncpg

from services import account_status as _acc_status
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


def _matched(rows) -> int:
    """Сколько аккаунтов ПОДОШЛО под правило, а не сколько строк вернул LIMIT.

    Каждая карточка здесь показывала владельцу `len(rows)` при `LIMIT 5`/`LIMIT 10`
    в запросе: на флоте из двухсот аккаунтов «Низкое доверие: 5 аккаунт(ов)»
    означало «минимум 5, а сколько на самом деле — неизвестно». Он читает это как
    точное число и решает по нему, лечить флот или нет. Запросы теперь возвращают
    настоящий счётчик окном COUNT(*) OVER (), и примеры по-прежнему обрезаны
    лимитом — обрезан список, не число.
    """
    if not rows:
        return 0
    try:
        total = rows[0]["match_cnt"]
    except (KeyError, TypeError, IndexError):
        return len(rows)
    return len(rows) if total is None else int(total)


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
        """SELECT id, phone, first_name, EXTRACT(EPOCH FROM (cooldown_until - NOW()))/3600 AS hours_left,
                  COUNT(*) OVER () AS match_cnt
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
        recs.append(
            {
                "severity": "warning",
                "icon": "⏳",
                "title": f"Долгий кулдаун: {_matched(cooling_long)} аккаунт(ов)",
                "text": f"Аккаунты {names} и другие заблокированы флудом на >6 часов. Снизьте интенсивность операций.",
                "action": "accounts",
            }
        )

    # 2. Accounts with many flood events (last 7d > 10)
    flood_heavy = await pool.fetch(
        """SELECT id, phone, first_name, flood_count_7d, COUNT(*) OVER () AS match_cnt
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE AND COALESCE(flood_count_7d,0) > 10
           ORDER BY flood_count_7d DESC LIMIT 5""",
        owner_id,
    )
    if flood_heavy:
        worst = flood_heavy[0]
        name = worst.get("first_name") or worst.get("phone") or f"id{worst['id']}"
        recs.append(
            {
                "severity": "warning",
                "icon": "🌊",
                "title": f"Высокая флуд-активность: {_matched(flood_heavy)} аккаунт(ов)",
                "text": f"Аккаунт {name} получил {worst['flood_count_7d']} флудов за 7 дней. Аккаунт перегружен — дайте ему отдохнуть.",
                "action": "accounts",
            }
        )

    # 3. Low trust accounts still in rotation
    low_trust = await pool.fetch(
        """SELECT id, phone, first_name, trust_score, COUNT(*) OVER () AS match_cnt
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
        recs.append(
            {
                "severity": "critical",
                "icon": "🚨",
                "title": f"Низкое доверие: {_matched(low_trust)} аккаунт(ов)",
                "text": f"Аккаунты {names} имеют trust_score < 0.3. Используйте их для некритичных операций или разогрейте.",
                "action": "warmup",
            }
        )

    # 4. Restricted/banned accounts not cleaned up
    restricted_rows = await pool.fetch(
        # Отбор — ровно тот, по которому операции отказываются брать аккаунт:
        # общий словарь мёртвых статусов плюс отсутствие сессии. Раньше здесь
        # стоял свой список из трёх статусов и `LIMIT 10` на ВСЕХ активных
        # аккаунтах: аккаунт с `deleted`, `frozen` или отозванной сессией в
        # карточку не попадал, а на флоте больше десяти аккаунтов правило
        # смотрело только на десять самых старых и молчало про остальных.
        """SELECT id, phone, first_name, COALESCE(acc_status,'active') AS acc_status,
                  (session_str IS NOT NULL AND session_str <> '') AS has_session
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE
             AND (session_str IS NULL OR session_str = ''
                  OR COALESCE(acc_status,'active') IN ("""
        + _acc_status.sql_dead_list() + """))
           ORDER BY added_at""",
        owner_id,
    )
    if restricted_rows:
        # Совет зависит от того, можно ли аккаунт вернуть. «Очистите их» на
        # аккаунте с отозванной сессией или спамблоком — это предложение
        # выбросить рабочий аккаунт: сессия переподключается, спамблок снимается.
        gone = [r for r in restricted_rows
                if r["acc_status"] in ("banned", "deactivated", "deleted")]
        reauth = [r for r in restricted_rows
                  if not r["has_session"] or r["acc_status"] == "session_expired"]
        waiting = [r for r in restricted_rows
                   if r["acc_status"] in ("spamblock", "frozen")]
        parts = []
        if gone:
            parts.append(f"выбыли безвозвратно (бан или удаление) — {len(gone)}: "
                         "их стоит отключить")
        if reauth:
            parts.append(f"потеряли сессию — {len(reauth)}: переподключите, "
                         "аккаунт при этом не теряется")
        if waiting:
            parts.append(f"под ограничением (спамблок или заморозка) — {len(waiting)}: "
                         "удалять не нужно, снимите ограничение или дайте отлежаться")
        recs.append(
            {
                "severity": "critical",
                "icon": "🚫",
                "title": f"Проблемные аккаунты: {len(restricted_rows)} шт",
                "text": ("Эти аккаунты числятся активными, но операции их не берут. "
                         + "; ".join(parts) + "."),
                "action": "cleaner",
            }
        )

    # 5. All accounts in one pool / no pool diversity
    pool_stats = await pool.fetch(
        "SELECT pool, COUNT(*) AS cnt FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE GROUP BY pool",
        owner_id,
    )
    null_pool_count = sum(r["cnt"] for r in pool_stats if r["pool"] is None)
    total_acc = sum(r["cnt"] for r in pool_stats)
    if total_acc >= 3 and null_pool_count == total_acc:
        recs.append(
            {
                "severity": "info",
                "icon": "🏊",
                "title": "Аккаунты не распределены по пулам",
                "text": "Назначьте аккаунтам пулы (strike, warmup, publish и т.д.) для умного распределения нагрузки.",
                "action": "accounts",
            }
        )

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
        recs.append(
            {
                "severity": "warning",
                "icon": "🌐",
                "title": f"Нестабильные прокси: {len(proxy_stats)} шт",   # без LIMIT — len честен
                "text": f"Прокси {names} имеют >30% ошибок за последние 7 дней. Замените или проверьте их.",
                "action": "proxies",
            }
        )

    # 7. Зависшие операции — по СОБСТВЕННОМУ потолку прогона, не по плоским 2 часам.
    #
    # Раньше карточка считала зависшей любую операцию, работающую дольше двух
    # часов. Потолок одного прогона — 6 часов, и массовый инвайт с пейсингом на
    # часы укладывается в него штатно: владельцу рисовали «зависшие операции»
    # ровно тогда, когда всё шло как надо. Ложная тревога здесь дороже молчания —
    # он отменяет живую операцию, а потом перестаёт верить и настоящим
    # предупреждениям. Порог теперь один на весь продукт
    # (op_worker.stuck_after_s), а выполняющиеся прямо сейчас операции
    # исключаются — тот же критерий, по которому их не трогает сторож воркера.
    import datetime as _dt_adv

    from services import op_worker as _ow_adv

    try:
        _active_now = await _ow_adv.active_op_ids()
    except Exception:
        _active_now = frozenset()
    _running = await pool.fetch(
        """SELECT id, op_type, COALESCE(started_at, created_at) AS since
             FROM operation_queue
            WHERE owner_id=$1 AND status='running'""",
        owner_id,
    )
    _now_adv = _dt_adv.datetime.now(_dt_adv.timezone.utc)
    stale_cnt = 0
    for _r in _running or ():
        if int(_r["id"]) in _active_now:
            continue
        _since = _r["since"]
        if _since is None:
            continue
        if _since.tzinfo is None:
            _since = _since.replace(tzinfo=_dt_adv.timezone.utc)
        if (_now_adv - _since).total_seconds() >= _ow_adv.stuck_after_s(_r["op_type"]):
            stale_cnt += 1
    if stale_cnt > 0:
        recs.append(
            {
                "severity": "warning",
                "icon": "🔄",
                "title": f"Зависшие операции: {stale_cnt} шт",
                "text": "Есть операции, которые работают дольше отведённого им "
                        "времени и не завершились. Проверьте очередь.",
                "action": "tasks",
            }
        )

    # 8. No active accounts at all
    active_count = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        owner_id,
    )
    if (active_count or 0) == 0:
        recs.append(
            {
                "severity": "critical",
                "icon": "📱",
                "title": "Нет активных аккаунтов",
                "text": "Добавьте хотя бы один аккаунт Telegram для использования операционных функций.",
                "action": "accounts",
            }
        )

    # 9. Accounts with poor memory performance (persistent failures from infra_memory_accounts)
    try:
        poor_memory = await pool.fetch(
            """SELECT ima.account_id,
                      COALESCE(a.first_name, a.phone, 'id'||ima.account_id::text) AS label,
                      ima.successes, ima.failures,
                      (ima.successes::float / NULLIF(ima.successes + ima.failures, 0)) AS success_rate,
                      COUNT(*) OVER () AS match_cnt
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
            recs.append(
                {
                    "severity": "warning",
                    "icon": "📉",
                    "title": f"Хроническая низкая эффективность: {_matched(poor_memory)} акк",
                    "text": (
                        f"Аккаунты {names} успешно выполняют <{worst_rate}% операций на основе "
                        f"исторических данных. Рекомендуется разогрев или исключение из активных операций."
                    ),
                    "action": "warmup",
                }
            )
    except Exception as e:
        log_exc_swallow(log, "_analyze")

    # 10. Pool concentration — one named pool has >80% of all accounts (uneven distribution)
    try:
        if total_acc >= 4:
            named_pools = [
                (r["pool"], r["cnt"]) for r in pool_stats if r["pool"] is not None
            ]
            for pool_name, cnt in named_pools:
                if cnt / total_acc > 0.80:
                    recs.append(
                        {
                            "severity": "info",
                            "icon": "⚖️",
                            "title": f"Дисбаланс пулов: {pool_name!r} перегружен",
                            "text": (
                                f"{cnt} из {total_acc} активных аккаунтов в пуле «{pool_name}». "
                                f"Распределите аккаунты по нескольким пулам для снижения точки отказа."
                            ),
                            "action": "accounts",
                        }
                    )
                    break
    except Exception as e:
        log_exc_swallow(log, "_analyze")

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
            recs.append(
                {
                    "severity": "warning",
                    "icon": "📛",
                    "title": f"Всплеск ошибок операций: {fail_pct}% за 48ч",
                    "text": (
                        f"{failed_recent} из {total_recent} последних операций завершились неудачей. "
                        f"Возможна перегрузка аккаунтов или проблемы с прокси."
                    ),
                    "action": "tasks",
                }
            )
    except Exception as e:
        log_exc_swallow(log, "_analyze")

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
                recs.append(
                    {
                        "severity": "info",
                        "icon": "🔌",
                        "title": f"Аккаунты без прокси: {no_proxy_accs} шт",
                        "text": (
                            f"У вас настроены прокси, но {no_proxy_accs} аккаунт(ов) работают без них. "
                            f"Назначьте прокси для защиты реального IP."
                        ),
                        "action": "proxies",
                    }
                )
    except Exception as e:
        log_exc_swallow(log, "_analyze")

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
            recs.append(
                {
                    "severity": "info",
                    "icon": "💤",
                    "title": f"Неиспользуемые надёжные аккаунты: {len(idle_high_trust)} шт",
                    "text": (
                        f"Аккаунты {names} имеют высокий trust_score, но не использовались >7 дней. "
                        f"Включите их в операции для максимальной эффективности."
                    ),
                    "action": "accounts",
                }
            )
    except Exception as e:
        log_exc_swallow(log, "_analyze")

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
