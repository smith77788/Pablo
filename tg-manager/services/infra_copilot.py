"""Infrastructure Copilot — автономный аналитический движок инфраструктуры.

BOTMOTHER ЭПОХА II: Infrastructure Copilot

Не просто рекомендации — реальные выводы из данных:
  - Паттерны деградации аккаунтов (за 7 дней)
  - Паттерны деградации прокси
  - Паттерны операционных сбоев
  - Риски ёмкости
  - Прогнозирование исходов операций

Все анализаторы устойчивы к отсутствию данных (новый пользователь с 0 операций).
Использует asyncio.gather для параллельных запросов.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field

import asyncpg

log = logging.getLogger(__name__)

# ── Snooze registry: owner_id → unix timestamp until which alerts are silenced ─
_snooze_until: dict[int, float] = {}


def snooze_alerts(owner_id: int, hours: float) -> None:
    """Заглушить уведомления Copilot для owner_id на указанное число часов."""
    import time

    _snooze_until[owner_id] = time.time() + hours * 3600


def is_snoozed(owner_id: int) -> bool:
    """True если алерты для owner_id сейчас заглушены."""
    import time

    exp = _snooze_until.get(owner_id, 0.0)
    if exp and time.time() < exp:
        return True
    _snooze_until.pop(owner_id, None)
    return False


def get_snooze_remaining(owner_id: int) -> str:
    """Возвращает читаемое время до конца снуза или пустую строку."""
    import time

    exp = _snooze_until.get(owner_id, 0.0)
    remaining = exp - time.time()
    if remaining <= 0:
        return ""
    hours, rem = divmod(int(remaining), 3600)
    mins = rem // 60
    if hours:
        return f"{hours}ч {mins}м"
    return f"{mins}м"


async def reload_snoozes_from_db(pool: asyncpg.Pool) -> None:
    """Загружает снуз-состояния из БД в оперативный словарь (вызывать при старте цикла)."""
    import time

    try:
        rows = await pool.fetch(
            "SELECT key, value FROM platform_settings WHERE key LIKE 'copilot_snooze_%'"
        )
        now = time.time()
        for row in rows:
            try:
                owner_id = int(row["key"].replace("copilot_snooze_", ""))
                exp = float(row["value"])
                if exp > now:
                    _snooze_until[owner_id] = exp
                else:
                    _snooze_until.pop(owner_id, None)
            except (ValueError, KeyError):
                pass
    except Exception as e:
        log.debug("reload_snoozes_from_db: %s", e)


# ── Severity порядок для сортировки ──────────────────────────────────────────
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "opportunity": 3}


# ── Датакласс инсайта ─────────────────────────────────────────────────────────


@dataclass
class CopilotInsight:
    category: str  # "account", "proxy", "queue", "pattern", "prediction"
    severity: str  # "critical", "warning", "info", "opportunity"
    title: str
    explanation: str  # WHY это проблема
    recommendation: str  # ЧТО сделать
    data: dict = field(default_factory=dict)  # supporting data
    score_impact: float = 0.0  # влияние на здоровье системы (0.0-1.0)


# ── Главная точка входа ───────────────────────────────────────────────────────


async def run_full_analysis(
    pool: asyncpg.Pool,
    owner_id: int,
    include_predictions: bool = True,
) -> list[CopilotInsight]:
    """Запустить все анализаторы параллельно и вернуть отсортированный список инсайтов."""
    try:
        tasks = [
            _analyze_account_patterns(pool, owner_id),
            _analyze_proxy_patterns(pool, owner_id),
            _analyze_operation_patterns(pool, owner_id),
            _analyze_capacity_risks(pool, owner_id),
            _analyze_memory_performance(pool, owner_id),
            _analyze_timing_patterns(pool, owner_id),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        insights: list[CopilotInsight] = []
        for r in results:
            if isinstance(r, Exception):
                log.warning("infra_copilot analyzer failed owner=%d: %s", owner_id, r)
                continue
            insights.extend(r)

        # Сортировка: сначала по severity, затем по score_impact убывающий
        insights.sort(
            key=lambda i: (
                _SEVERITY_ORDER.get(i.severity, 99),
                -i.score_impact,
            )
        )
        return insights
    except Exception as e:
        log.warning("infra_copilot run_full_analysis failed owner=%d: %s", owner_id, e)
        return []


# ── Анализатор аккаунтов ──────────────────────────────────────────────────────


async def _analyze_account_patterns(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ паттернов деградации аккаунтов."""
    insights: list[CopilotInsight] = []

    # 1. Аккаунты с деградирующим trust_score за 7 дней
    try:
        degraded = await pool.fetch(
            """SELECT a.id, COALESCE(a.first_name, a.phone, 'id'||a.id::text) AS label,
                      a.trust_score,
                      h7.avg_score AS score_7d_ago
               FROM tg_accounts a
               JOIN account_health_history h7
                 ON h7.account_id = a.id
                    AND h7.recorded_at < NOW() - INTERVAL '6 days'
                    AND h7.recorded_at > NOW() - INTERVAL '8 days'
               WHERE a.owner_id = $1
                 AND a.is_active = TRUE
                 AND COALESCE(a.trust_score, 1.0) < h7.avg_score - 0.1
               ORDER BY (h7.avg_score - COALESCE(a.trust_score, 1.0)) DESC
               LIMIT 5""",
            owner_id,
        )
        if degraded:
            names = ", ".join(r["label"] for r in degraded[:3])
            worst = degraded[0]
            delta = round(
                (worst.get("score_7d_ago") or 0) - (worst.get("trust_score") or 0), 2
            )
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="warning",
                    title=f"Деградация доверия: {len(degraded)} акк.",
                    explanation=(
                        f"Trust score аккаунтов {names} снизился за 7 дней. "
                        f"Худший: -{delta:.2f} пунктов."
                    ),
                    recommendation="Дайте аккаунтам отдохнуть 24-48ч, снизьте интенсивность операций.",
                    data={
                        "accounts": [dict(r) for r in degraded],
                        "count": len(degraded),
                    },
                    score_impact=0.5 + min(0.4, len(degraded) * 0.1),
                )
            )
    except Exception as e:
        log.debug("copilot degraded trust query failed: %s", e)

    # 2. Аккаунты с flood_count_7d > 5 и trust_score < 0.5 одновременно
    try:
        high_risk = await pool.fetch(
            """SELECT id, COALESCE(first_name, phone, 'id'||id::text) AS label,
                      trust_score, COALESCE(flood_count_7d, 0) AS flood_count_7d
               FROM tg_accounts
               WHERE owner_id = $1
                 AND is_active = TRUE
                 AND COALESCE(flood_count_7d, 0) > 5
                 AND COALESCE(trust_score, 1.0) < 0.5
               ORDER BY flood_count_7d DESC
               LIMIT 5""",
            owner_id,
        )
        if high_risk:
            names = ", ".join(r["label"] for r in high_risk[:3])
            worst = high_risk[0]
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="critical",
                    title=f"Двойной риск: {len(high_risk)} акк. (флуд + низкий trust)",
                    explanation=(
                        f"Аккаунты {names} одновременно имеют высокую флуд-активность "
                        f"(более 5 за 7д) и низкий trust_score (ниже 0.5). "
                        f"Худший: {worst['label']} — {worst['flood_count_7d']} флудов, "
                        f"trust={round(worst['trust_score'] or 0, 2)}."
                    ),
                    recommendation=(
                        "Выведите эти аккаунты из ротации немедленно. "
                        "Замените на свежие или начните прогрев новых."
                    ),
                    data={
                        "accounts": [dict(r) for r in high_risk],
                        "count": len(high_risk),
                    },
                    score_impact=0.9,
                )
            )
    except Exception as e:
        log.debug("copilot high_risk combined query failed: %s", e)

    # 3. Аккаунты где последние 3 операции были ошибками (через operation_audit)
    try:
        # Graceful fallback если таблицы нет
        recent_fail_accs = await pool.fetch(
            """SELECT account_id,
                      COALESCE(a.first_name, a.phone, 'id'||a.id::text) AS label,
                      COUNT(*) AS fail_count
               FROM operation_audit oa
               JOIN tg_accounts a ON a.id = oa.account_id AND a.owner_id = $1
               WHERE oa.owner_id = $1
                 AND oa.success = FALSE
                 AND oa.created_at > NOW() - INTERVAL '3 days'
               GROUP BY account_id, a.first_name, a.phone, a.id
               HAVING COUNT(*) >= 3
               ORDER BY fail_count DESC
               LIMIT 5""",
            owner_id,
        )
        if recent_fail_accs:
            names = ", ".join(r["label"] for r in recent_fail_accs[:3])
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="warning",
                    title=f"Серия ошибок: {len(recent_fail_accs)} акк.",
                    explanation=(
                        f"Аккаунты {names} имеют 3+ ошибки подряд за последние 3 дня. "
                        f"Паттерн систематических сбоев."
                    ),
                    recommendation=(
                        "Проверьте статус аккаунтов через Account Monitor. "
                        "Возможно, сессии устарели или аккаунты получили ограничения."
                    ),
                    data={
                        "accounts": [dict(r) for r in recent_fail_accs],
                        "count": len(recent_fail_accs),
                    },
                    score_impact=0.6,
                )
            )
    except Exception as e:
        # operation_audit может не существовать — это нормально
        log.debug(
            "copilot operation_audit query failed (table may not exist): %s",
            type(e).__name__,
        )

    # 4. Недоиспользуемые аккаунты (last_used > 7 дней при trust_score > 0.7)
    try:
        underused = await pool.fetch(
            """SELECT id, COALESCE(first_name, phone, 'id'||id::text) AS label,
                      trust_score,
                      EXTRACT(DAY FROM NOW() - last_used) AS days_idle
               FROM tg_accounts
               WHERE owner_id = $1
                 AND is_active = TRUE
                 AND COALESCE(trust_score, 1.0) > 0.7
                 AND last_used < NOW() - INTERVAL '7 days'
               ORDER BY trust_score DESC
               LIMIT 5""",
            owner_id,
        )
        if underused:
            names = ", ".join(r["label"] for r in underused[:3])
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="opportunity",
                    title=f"Неиспользуемые ресурсы: {len(underused)} акк.",
                    explanation=(
                        f"Аккаунты {names} с высоким trust (>0.7) не использовались "
                        f"более 7 дней. Простаивающий ресурс."
                    ),
                    recommendation=(
                        "Включите эти аккаунты в операции — они хорошо прогреты "
                        "и готовы к высокой нагрузке."
                    ),
                    data={
                        "accounts": [dict(r) for r in underused],
                        "count": len(underused),
                    },
                    score_impact=0.2,
                )
            )
    except Exception as e:
        log.debug("copilot underused accounts query failed: %s", e)

    return insights


# ── Анализатор прокси ─────────────────────────────────────────────────────────


async def _analyze_proxy_patterns(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ паттернов деградации прокси."""
    insights: list[CopilotInsight] = []

    # 1. Деградирующие прокси (success rate падает)
    try:
        degrading = await pool.fetch(
            """WITH daily AS (
               SELECT pql.proxy_id,
                      DATE(pql.checked_at) AS day,
                      COUNT(*) FILTER (WHERE pql.success) * 1.0 / NULLIF(COUNT(*), 0) AS rate
               FROM proxy_quality_log pql
               JOIN user_proxies up ON up.id = pql.proxy_id AND up.owner_id = $1
               WHERE pql.checked_at > NOW() - INTERVAL '3 days'
               GROUP BY pql.proxy_id, DATE(pql.checked_at)
               HAVING COUNT(*) >= 3
            )
            SELECT d1.proxy_id,
                   up.label,
                   d1.rate AS rate_latest,
                   d3.rate AS rate_3d_ago
               FROM daily d1
               JOIN daily d3 ON d3.proxy_id = d1.proxy_id
                             AND d3.day < d1.day - INTERVAL '1 day'
               JOIN user_proxies up ON up.id = d1.proxy_id
               WHERE d1.day = (SELECT MAX(day) FROM daily WHERE proxy_id = d1.proxy_id)
                 AND d3.day = (SELECT MIN(day) FROM daily WHERE proxy_id = d1.proxy_id)
                 AND d1.rate < d3.rate - 0.15
               ORDER BY (d3.rate - d1.rate) DESC
               LIMIT 5""",
            owner_id,
        )
        if degrading:
            names = ", ".join(
                (r["label"] or f"proxy#{r['proxy_id']}") for r in degrading[:3]
            )
            insights.append(
                CopilotInsight(
                    category="proxy",
                    severity="warning",
                    title=f"Деградация прокси: {len(degrading)} шт.",
                    explanation=(
                        f"Прокси {names} показывают падение success rate за последние 3 дня. "
                        f"Тренд ухудшения устойчивый."
                    ),
                    recommendation=(
                        "Замените деградирующие прокси или проверьте провайдера. "
                        "Убедитесь что IP не попал в блокировки Telegram."
                    ),
                    data={
                        "proxies": [dict(r) for r in degrading],
                        "count": len(degrading),
                    },
                    score_impact=0.5,
                )
            )
    except Exception as e:
        log.debug("copilot degrading proxies query failed: %s", e)

    # 2. Прокси с высокой латентностью (> 2000ms)
    try:
        slow_proxies = await pool.fetch(
            """SELECT up.id, up.label,
                      AVG(pql.latency_ms) AS avg_latency
               FROM proxy_quality_log pql
               JOIN user_proxies up ON up.id = pql.proxy_id AND up.owner_id = $1
               WHERE pql.checked_at > NOW() - INTERVAL '7 days'
                 AND pql.success = TRUE
               GROUP BY up.id, up.label
               HAVING AVG(pql.latency_ms) > 2000
               ORDER BY avg_latency DESC
               LIMIT 5""",
            owner_id,
        )
        if slow_proxies:
            names = ", ".join(
                (r["label"] or f"proxy#{r['id']}") for r in slow_proxies[:3]
            )
            worst_latency = round(slow_proxies[0].get("avg_latency") or 0)
            insights.append(
                CopilotInsight(
                    category="proxy",
                    severity="warning",
                    title=f"Медленные прокси: {len(slow_proxies)} шт.",
                    explanation=(
                        f"Прокси {names} имеют среднюю задержку >2000ms. "
                        f"Максимальная: {worst_latency}ms. "
                        f"Высокая латентность увеличивает время операций и риск таймаутов."
                    ),
                    recommendation=(
                        "Смените провайдера прокси или регион. "
                        "Идеальная латентность для Telegram API: до 500ms."
                    ),
                    data={
                        "proxies": [dict(r) for r in slow_proxies],
                        "count": len(slow_proxies),
                    },
                    score_impact=0.4,
                )
            )
    except Exception as e:
        log.debug("copilot slow proxies query failed: %s", e)

    # 3. Аккаунты без прокси (если у других есть — риск неравномерности)
    try:
        proxy_coverage = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE is_active) AS total,
                   COUNT(*) FILTER (WHERE is_active AND proxy_id IS NOT NULL) AS with_proxy
               FROM tg_accounts
               WHERE owner_id = $1""",
            owner_id,
        )
        if proxy_coverage:
            total = proxy_coverage["total"] or 0
            with_proxy = proxy_coverage["with_proxy"] or 0
            without_proxy = total - with_proxy
            if total >= 3 and with_proxy > 0 and without_proxy > 0:
                coverage_pct = round(with_proxy / total * 100)
                insights.append(
                    CopilotInsight(
                        category="proxy",
                        severity="info",
                        title=f"Неравномерное покрытие прокси: {coverage_pct}%",
                        explanation=(
                            f"{without_proxy} из {total} активных аккаунтов работают без прокси, "
                            f"хотя у {with_proxy} прокси есть. "
                            f"Неравномерность создаёт риск — аккаунты без прокси могут быть "
                            f"идентифицированы по IP."
                        ),
                        recommendation=(
                            "Назначьте прокси всем аккаунтам или осознанно разделите "
                            "их на пулы с разными политиками."
                        ),
                        data={
                            "total": total,
                            "with_proxy": with_proxy,
                            "without_proxy": without_proxy,
                            "coverage_pct": coverage_pct,
                        },
                        score_impact=0.3,
                    )
                )
    except Exception as e:
        log.debug("copilot proxy coverage query failed: %s", e)

    return insights


# ── Анализатор операций ───────────────────────────────────────────────────────


async def _analyze_operation_patterns(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ операционных паттернов сбоев."""
    insights: list[CopilotInsight] = []

    # 1. Операции с retry_count > 2 — паттерн системных сбоев
    try:
        high_retry = await pool.fetch(
            """SELECT op_type,
                      COUNT(*) AS cnt,
                      AVG(retry_count) AS avg_retry
               FROM operation_queue
               WHERE owner_id = $1
                 AND retry_count > 2
                 AND created_at > NOW() - INTERVAL '7 days'
               GROUP BY op_type
               ORDER BY cnt DESC
               LIMIT 5""",
            owner_id,
        )
        if high_retry:
            types_list = ", ".join(r["op_type"] for r in high_retry[:3])
            total_cnt = sum(r["cnt"] for r in high_retry)
            insights.append(
                CopilotInsight(
                    category="pattern",
                    severity="warning",
                    title=f"Систематические повторы: {total_cnt} операций",
                    explanation=(
                        f"Операции типа {types_list} требуют >2 повторных попыток. "
                        f"Это признак системной нестабильности — сессий, прокси или API."
                    ),
                    recommendation=(
                        "Проверьте состояние аккаунтов и прокси, используемых в этих операциях. "
                        "Возможно, часть сессий устарела."
                    ),
                    data={
                        "op_types": [dict(r) for r in high_retry],
                        "total_count": total_cnt,
                    },
                    score_impact=0.5,
                )
            )
    except Exception as e:
        log.debug("copilot high retry query failed: %s", e)

    # 2. Типы операций с failure rate > 30% за последние 7 дней
    try:
        failed_types = await pool.fetch(
            """SELECT op_type,
                      COUNT(*) FILTER (WHERE status = 'failed') AS failed_cnt,
                      COUNT(*) AS total_cnt,
                      COUNT(*) FILTER (WHERE status = 'failed') * 100.0
                        / NULLIF(COUNT(*), 0) AS fail_rate
               FROM operation_queue
               WHERE owner_id = $1
                 AND created_at > NOW() - INTERVAL '7 days'
                 AND status IN ('done', 'failed')
               GROUP BY op_type
               HAVING COUNT(*) >= 3
                  AND COUNT(*) FILTER (WHERE status = 'failed') * 100.0
                      / NULLIF(COUNT(*), 0) > 30
               ORDER BY fail_rate DESC
               LIMIT 5""",
            owner_id,
        )
        if failed_types:
            worst = failed_types[0]
            types_list = ", ".join(r["op_type"] for r in failed_types)
            insights.append(
                CopilotInsight(
                    category="pattern",
                    severity="warning",
                    title=f"Высокий failure rate: {len(failed_types)} тип(а) операций",
                    explanation=(
                        f"Операции {types_list} за последние 7 дней имеют >30% ошибок. "
                        f"Худший: {worst['op_type']} — {round(worst['fail_rate'] or 0)}% отказов "
                        f"({worst['failed_cnt']}/{worst['total_cnt']})."
                    ),
                    recommendation=(
                        "Исследуйте логи операций этого типа. Часто причина — флуд-лимиты, "
                        "нехватка аккаунтов или изменения в Telegram API."
                    ),
                    data={
                        "op_types": [dict(r) for r in failed_types],
                    },
                    score_impact=0.6,
                )
            )
    except Exception as e:
        log.debug("copilot failed op types query failed: %s", e)

    # 3. Время дня с наибольшей частотой ошибок (из operation_audit если есть)
    try:
        error_hours = await pool.fetch(
            """SELECT EXTRACT(HOUR FROM created_at)::int AS hour,
                      COUNT(*) FILTER (WHERE success = FALSE) AS errors,
                      COUNT(*) AS total
               FROM operation_audit
               WHERE owner_id = $1
                 AND created_at > NOW() - INTERVAL '7 days'
               GROUP BY EXTRACT(HOUR FROM created_at)
               HAVING COUNT(*) >= 5
                  AND COUNT(*) FILTER (WHERE success = FALSE) * 1.0 / NULLIF(COUNT(*), 0) > 0.3
               ORDER BY errors DESC
               LIMIT 3""",
            owner_id,
        )
        if error_hours:
            worst_hour = error_hours[0]
            hour_val = worst_hour["hour"]
            error_rate = round(
                (worst_hour["errors"] or 0) / max(worst_hour["total"] or 1, 1) * 100
            )
            peak_hours = ", ".join(f"{r['hour']:02d}:00" for r in error_hours)
            insights.append(
                CopilotInsight(
                    category="pattern",
                    severity="info",
                    title=f"Пиковые часы ошибок: {peak_hours}",
                    explanation=(
                        f"В {hour_val:02d}:00 зафиксирован наивысший процент ошибок: {error_rate}%. "
                        f"Паттерн повторяется последние 7 дней."
                    ),
                    recommendation=(
                        f"Избегайте запуска критичных операций в {peak_hours}. "
                        f"Перенесите их на ночное время или ранее утро."
                    ),
                    data={
                        "error_hours": [dict(r) for r in error_hours],
                        "peak_hour": hour_val,
                        "peak_error_rate": error_rate,
                    },
                    score_impact=0.3,
                )
            )
    except Exception as e:
        # operation_audit может не существовать
        log.debug(
            "copilot error hours query failed (table may not exist): %s",
            type(e).__name__,
        )

    return insights


# ── Анализатор ёмкости ────────────────────────────────────────────────────────


async def _analyze_capacity_risks(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ рисков ёмкости инфраструктуры."""
    insights: list[CopilotInsight] = []

    # Получаем базовые данные параллельно
    try:
        active_task, queue_task, pool_task = await asyncio.gather(
            pool.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE is_active AND
                           (cooldown_until IS NULL OR cooldown_until < NOW())) AS ready,
                       COUNT(*) FILTER (WHERE is_active) AS total,
                       COUNT(DISTINCT COALESCE(pool, '__none__')) AS pool_count
                   FROM tg_accounts
                   WHERE owner_id = $1""",
                owner_id,
            ),
            pool.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                       COUNT(*) FILTER (WHERE status = 'running') AS running
                   FROM operation_queue
                   WHERE owner_id = $1""",
                owner_id,
            ),
            pool.fetch(
                "SELECT pool, COUNT(*) AS cnt FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE GROUP BY pool",
                owner_id,
            ),
            return_exceptions=True,
        )
    except Exception as e:
        log.debug("copilot capacity gather failed: %s", e)
        return insights

    # 1. Если < 2 активных аккаунтов — критический риск
    if not isinstance(active_task, Exception) and active_task:
        total = active_task.get("total") or 0
        ready = active_task.get("ready") or 0

        if total == 0:
            insights.append(
                CopilotInsight(
                    category="queue",
                    severity="critical",
                    title="Нет активных аккаунтов",
                    explanation=(
                        "В системе нет ни одного активного аккаунта. "
                        "Все операции будут завершаться с ошибкой."
                    ),
                    recommendation=(
                        "Добавьте хотя бы один аккаунт Telegram через раздел Аккаунты."
                    ),
                    data={"total": total, "ready": ready},
                    score_impact=1.0,
                )
            )
        elif ready < 2:
            insights.append(
                CopilotInsight(
                    category="queue",
                    severity="critical",
                    title=f"Критически мало готовых аккаунтов: {ready}/{total}",
                    explanation=(
                        f"Только {ready} аккаунт(а) готовы к работе прямо сейчас. "
                        f"Остальные {total - ready} находятся в кулдауне. "
                        f"Параллельные операции невозможны."
                    ),
                    recommendation=(
                        "Добавьте новые аккаунты или дождитесь окончания кулдауна. "
                        "Рекомендуется минимум 3-5 готовых аккаунтов для надёжной работы."
                    ),
                    data={"total": total, "ready": ready, "in_cooldown": total - ready},
                    score_impact=0.9,
                )
            )

    # 2. Если pending_queue > running_capacity * 3
    if (
        not isinstance(queue_task, Exception)
        and queue_task
        and not isinstance(active_task, Exception)
        and active_task
    ):
        pending = queue_task.get("pending") or 0
        running = queue_task.get("running") or 0
        ready_accs = active_task.get("ready") or 0
        running_capacity = max(ready_accs, 1)

        if pending > running_capacity * 3:
            queue_ratio = round(pending / running_capacity, 1)
            insights.append(
                CopilotInsight(
                    category="queue",
                    severity="warning",
                    title=f"Перегрузка очереди: {pending} задач в ожидании",
                    explanation=(
                        f"Очередь операций переполнена: {pending} задач ждут, "
                        f"при этом только {ready_accs} аккаунтов готовы к работе. "
                        f"Коэффициент загрузки: {queue_ratio}x от ёмкости."
                    ),
                    recommendation=(
                        "Добавьте аккаунты для увеличения параллельной ёмкости, "
                        "или очистите очередь от неактуальных задач."
                    ),
                    data={
                        "pending": pending,
                        "running": running,
                        "ready_accounts": ready_accs,
                        "queue_ratio": queue_ratio,
                    },
                    score_impact=0.7,
                )
            )

    # 3. Если все аккаунты в одном пуле — риск единой точки отказа
    if (
        not isinstance(pool_task, Exception)
        and pool_task
        and not isinstance(active_task, Exception)
        and active_task
    ):
        total = active_task.get("total") or 0
        if total >= 4 and len(pool_task) == 1:
            only_pool = pool_task[0]["pool"] or "без пула"
            insights.append(
                CopilotInsight(
                    category="queue",
                    severity="info",
                    title="Все аккаунты в одном пуле — единая точка отказа",
                    explanation=(
                        f"Все {total} активных аккаунтов находятся в пуле '{only_pool}'. "
                        f"При проблемах с этим пулом вся инфраструктура выйдет из строя."
                    ),
                    recommendation=(
                        "Разделите аккаунты на несколько пулов: "
                        "primary (основные), warmup (прогрев), reserve (резерв)."
                    ),
                    data={
                        "total": total,
                        "pool_name": only_pool,
                        "pool_count": 1,
                    },
                    score_impact=0.35,
                )
            )

    return insights


# ── Анализатор Memory Performance ─────────────────────────────────────────────


async def _analyze_memory_performance(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ исторической эффективности аккаунтов через infra_memory_accounts."""
    insights: list[CopilotInsight] = []

    # 1. Аккаунты с хронически плохой эффективностью (>20 операций, <30% успеха)
    try:
        poor = await pool.fetch(
            """SELECT ima.account_id,
                      COALESCE(a.first_name, a.phone, 'id'||ima.account_id::text) AS label,
                      ima.successes, ima.failures,
                      (ima.successes + ima.failures) AS total,
                      (ima.successes::float / NULLIF(ima.successes + ima.failures, 0)) AS success_rate
               FROM infra_memory_accounts ima
               JOIN tg_accounts a ON a.id = ima.account_id
               WHERE a.owner_id=$1 AND a.is_active=TRUE
                 AND (ima.successes + ima.failures) >= 20
                 AND (ima.successes::float / (ima.successes + ima.failures)) < 0.30
               ORDER BY success_rate ASC LIMIT 5""",
            owner_id,
        )
        if poor:
            labels = ", ".join(r["label"] for r in poor[:3])
            worst_rate = int((poor[0]["success_rate"] or 0) * 100)
            worst_total = poor[0]["total"]
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="warning",
                    title=f"Хроническая низкая эффективность: {len(poor)} акк ({worst_rate}% успеха)",
                    explanation=(
                        f"Аккаунты {labels} показывают <30% успешных операций "
                        f"(худший: {worst_rate}% из {worst_total} операций). "
                        f"Это не флуд-ограничения — это системная проблема эффективности."
                    ),
                    recommendation=(
                        "Разогрейте проблемные аккаунты или переведите в пул 'warmup'. "
                        "Не используйте их в критичных операциях."
                    ),
                    data={"count": len(poor), "worst_rate": worst_rate},
                    score_impact=0.55,
                )
            )
    except Exception as e:
        log.debug(
            "_analyze_memory_performance poor query failed owner=%d: %s", owner_id, e
        )

    # 2. Trust-memory divergence — аккаунты с высоким trust но низкой памятью
    try:
        diverged = await pool.fetch(
            """SELECT a.id, COALESCE(a.first_name, a.phone, 'id'||a.id::text) AS label,
                      a.trust_score,
                      (ima.successes::float / NULLIF(ima.successes + ima.failures, 0)) AS mem_rate,
                      (ima.successes + ima.failures) AS total
               FROM tg_accounts a
               JOIN infra_memory_accounts ima ON ima.account_id = a.id
               WHERE a.owner_id=$1 AND a.is_active=TRUE
                 AND COALESCE(a.trust_score, 0) > 0.65
                 AND (ima.successes + ima.failures) >= 15
                 AND (ima.successes::float / (ima.successes + ima.failures)) < 0.40
               ORDER BY (COALESCE(a.trust_score, 0) - ima.successes::float / (ima.successes + ima.failures)) DESC
               LIMIT 3""",
            owner_id,
        )
        if diverged:
            first = diverged[0]
            label = first["label"]
            trust = int((first["trust_score"] or 0) * 100)
            mem_rate = int((first["mem_rate"] or 0) * 100)
            insights.append(
                CopilotInsight(
                    category="account",
                    severity="info",
                    title="Расхождение: высокий trust, низкая реальная эффективность",
                    explanation=(
                        f"Аккаунт {label}: trust {trust}%, но реальная эффективность "
                        f"{mem_rate}% по данным памяти. Метрика trust может быть устаревшей."
                    ),
                    recommendation=(
                        "Проверьте аккаунт реальной проверкой. "
                        "Trust score обновляется периодически, память — в реальном времени."
                    ),
                    data={"account_label": label, "trust": trust, "mem_rate": mem_rate},
                    score_impact=0.30,
                )
            )
    except Exception as e:
        log.debug(
            "_analyze_memory_performance divergence query failed owner=%d: %s",
            owner_id,
            e,
        )

    return insights


# ── Анализатор паттернов времени ──────────────────────────────────────────────


async def _analyze_timing_patterns(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[CopilotInsight]:
    """Анализ паттернов времени операций из infra_memory для рекомендаций по расписанию."""
    insights: list[CopilotInsight] = []

    # Сравниваем success rate по часам суток из operation_queue
    try:
        hour_stats = await pool.fetch(
            """SELECT
                   EXTRACT(HOUR FROM created_at)::integer AS hour,
                   COUNT(*) FILTER (WHERE status='done') AS successes,
                   COUNT(*) AS total
               FROM operation_queue
               WHERE owner_id=$1 AND created_at > NOW() - INTERVAL '14 days'
                 AND status IN ('done', 'failed', 'error')
               GROUP BY hour
               HAVING COUNT(*) >= 3
               ORDER BY hour""",
            owner_id,
        )
        if len(hour_stats) >= 4:
            # Find best and worst hours
            rates = {
                r["hour"]: (r["successes"] or 0) / max(r["total"], 1)
                for r in hour_stats
            }
            best_hour = max(rates, key=rates.__getitem__)
            worst_hour = min(rates, key=rates.__getitem__)
            best_rate = int(rates[best_hour] * 100)
            worst_rate = int(rates[worst_hour] * 100)
            spread = best_rate - worst_rate

            if spread >= 25 and best_rate >= 70:
                insights.append(
                    CopilotInsight(
                        category="pattern",
                        severity="info",
                        title=f"Оптимальное время для операций: {best_hour:02d}:00",
                        explanation=(
                            f"За последние 14 дней в {best_hour:02d}:00 успех {best_rate}%, "
                            f"а в {worst_hour:02d}:00 — только {worst_rate}%. "
                            f"Разброс {spread}% указывает на значимые временны́е паттерны."
                        ),
                        recommendation=(
                            f"Планируйте критичные операции на {best_hour:02d}:00–{(best_hour + 2) % 24:02d}:00. "
                            f"Избегайте запуска в {worst_hour:02d}:00."
                        ),
                        data={
                            "best_hour": best_hour,
                            "worst_hour": worst_hour,
                            "best_rate": best_rate,
                            "worst_rate": worst_rate,
                        },
                        score_impact=0.20,
                    )
                )
    except Exception as e:
        log.debug("_analyze_timing_patterns failed owner=%d: %s", owner_id, e)

    return insights


# ── Прогнозирование исходов операций ─────────────────────────────────────────


async def predict_operation_outcome(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    item_count: int,
) -> dict:
    """Прогнозировать исход операции до её запуска.

    Возвращает:
        estimated_duration_minutes — ожидаемая длительность
        success_probability       — вероятность успеха (0.0-1.0)
        expected_errors           — ожидаемое число ошибок
        risk_level                — "low" | "medium" | "high" | "critical"
        risk_reasons              — список причин риска
        recommended_accounts      — оптимальное число аккаунтов
        warnings                  — предупреждения
    """
    # Значения по умолчанию (безопасные для нового пользователя)
    result = {
        "estimated_duration_minutes": None,
        "success_probability": 0.7,
        "expected_errors": 0,
        "risk_level": "low",
        "risk_reasons": [],
        "recommended_accounts": max(1, (item_count // 20) + 1),
        "warnings": [],
    }
    risk_reasons: list[str] = []
    warnings: list[str] = []
    risk_score = 0.0

    # 1. Исторический success rate по op_type за 7 дней
    try:
        hist = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status = 'done') AS completed,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                   AVG(EXTRACT(EPOCH FROM (finished_at - created_at)) / 60.0)
                     FILTER (WHERE status = 'done' AND total_items > 0)
                       AS avg_duration_min,
                   AVG(total_items)
                     FILTER (WHERE status = 'done') AS avg_items
               FROM operation_queue
               WHERE owner_id = $1
                 AND op_type = $2
                 AND created_at > NOW() - INTERVAL '7 days'
                 AND status IN ('done', 'failed')""",
            owner_id,
            op_type,
        )
        if hist:
            total_hist = (hist.get("completed") or 0) + (hist.get("failed") or 0)
            if total_hist >= 2:
                completed = hist.get("completed") or 0
                success_prob = completed / total_hist
                result["success_probability"] = round(success_prob, 3)

                avg_dur = hist.get("avg_duration_min")
                avg_items = hist.get("avg_items") or 1
                if avg_dur and avg_items and avg_items > 0:
                    estimated = round(avg_dur * (item_count / avg_items), 1)
                    result["estimated_duration_minutes"] = estimated

                if success_prob < 0.5:
                    risk_score += 0.4
                    risk_reasons.append(
                        f"Исторический успех только {round(success_prob * 100)}%"
                    )
                elif success_prob < 0.7:
                    risk_score += 0.2
                    risk_reasons.append(
                        f"Умеренный исторический успех: {round(success_prob * 100)}%"
                    )
    except Exception as e:
        log.debug("copilot predict history query failed: %s", e)

    # 2. Текущее состояние аккаунтов
    try:
        acc_state = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE is_active AND
                       (cooldown_until IS NULL OR cooldown_until < NOW())) AS ready,
                   COUNT(*) FILTER (WHERE is_active) AS total,
                   AVG(COALESCE(trust_score, 1.0)) FILTER (WHERE is_active) AS avg_trust,
                   AVG(COALESCE(flood_count_7d, 0)) FILTER (WHERE is_active) AS avg_flood
               FROM tg_accounts
               WHERE owner_id = $1""",
            owner_id,
        )
        if acc_state:
            ready = acc_state.get("ready") or 0
            avg_trust = float(acc_state.get("avg_trust") or 0.7)
            avg_flood = float(acc_state.get("avg_flood") or 0)

            if ready == 0:
                risk_score += 0.5
                risk_reasons.append("Нет готовых аккаунтов")
                warnings.append(
                    "Операция не может быть запущена — все аккаунты в кулдауне"
                )
            elif ready < 2:
                risk_score += 0.3
                risk_reasons.append(f"Только {ready} аккаунт готов")

            if avg_trust < 0.4:
                risk_score += 0.3
                risk_reasons.append(f"Низкий средний trust: {round(avg_trust, 2)}")
            elif avg_trust < 0.6:
                risk_score += 0.15
                risk_reasons.append(f"Умеренный средний trust: {round(avg_trust, 2)}")

            if avg_flood > 8:
                risk_score += 0.2
                risk_reasons.append(
                    f"Высокая флуд-активность: {round(avg_flood, 1)}/7д"
                )

            # Рекомендованное число аккаунтов
            if item_count <= 10:
                recommended = 1
            elif item_count <= 50:
                recommended = min(ready, 2)
            elif item_count <= 200:
                recommended = min(ready, 3)
            else:
                recommended = min(ready, max(3, item_count // 50))
            result["recommended_accounts"] = max(1, recommended)
    except Exception as e:
        log.debug("copilot predict acc state query failed: %s", e)

    # 3. Текущая нагрузка очереди
    try:
        queue_state = await pool.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status = 'running') AS running,
                      COUNT(*) FILTER (WHERE status = 'pending') AS pending
               FROM operation_queue
               WHERE owner_id = $1""",
            owner_id,
        )
        if queue_state:
            running_ops = queue_state.get("running") or 0
            pending_ops = queue_state.get("pending") or 0
            if running_ops >= 3:
                risk_score += 0.15
                warnings.append(f"Уже запущено {running_ops} параллельных операций")
            if pending_ops > 5:
                warnings.append(f"В очереди ожидает {pending_ops} операций")
    except Exception as e:
        log.debug("copilot predict queue state query failed: %s", e)

    # 4. Большой объём операции
    if item_count > 500:
        risk_score += 0.2
        warnings.append(f"Большой объём операции: {item_count} элементов")
        if result.get("estimated_duration_minutes") is None:
            # Примерная оценка: ~3 элемента/мин при умеренном темпе
            result["estimated_duration_minutes"] = round(item_count / 3, 1)
    elif item_count > 100:
        warnings.append(f"Объём операции: {item_count} элементов — займёт время")
        if result.get("estimated_duration_minutes") is None:
            result["estimated_duration_minutes"] = round(item_count / 5, 1)

    # 5. Итоговый уровень риска
    if risk_score >= 0.7:
        risk_level = "critical"
    elif risk_score >= 0.45:
        risk_level = "high"
    elif risk_score >= 0.2:
        risk_level = "medium"
    else:
        risk_level = "low"

    result["risk_level"] = risk_level
    result["risk_reasons"] = risk_reasons
    result["warnings"] = warnings

    success_prob = result.get("success_probability") or 0.7
    result["expected_errors"] = round(item_count * (1 - success_prob), 1)

    return result


# ── Форматирование отчётов ────────────────────────────────────────────────────


def format_copilot_report(
    insights: list[CopilotInsight],
    max_items: int = 5,
) -> str:
    """HTML-форматированный отчёт для Telegram. Critical первыми."""
    if not insights:
        return "✅ <b>Copilot не нашёл проблем</b> — инфраструктура в норме."

    _SEVERITY_ICONS = {
        "critical": "🚨",
        "warning": "⚠️",
        "info": "ℹ️",
        "opportunity": "💡",
    }

    lines = ["🤖 <b>Infrastructure Copilot</b>\n"]

    shown = insights[:max_items]
    for ins in shown:
        icon = _SEVERITY_ICONS.get(ins.severity, "•")
        title = html.escape(ins.title)
        explanation = html.escape(ins.explanation)
        recommendation = html.escape(ins.recommendation)
        impact_bar = "█" * round(ins.score_impact * 5) + "░" * (
            5 - round(ins.score_impact * 5)
        )
        lines.append(
            f"{icon} <b>{title}</b>\n"
            f"   📌 {explanation}\n"
            f"   ✅ {recommendation}\n"
            f"   Влияние: [{impact_bar}]\n"
        )

    if len(insights) > max_items:
        remaining = len(insights) - max_items
        lines.append(f"<i>...и ещё {remaining} инсайт(ов)</i>")

    return "\n".join(lines).strip()


def format_pre_launch_intelligence(
    prediction: dict,
    insights: list[CopilotInsight],
) -> str:
    """Компактный блок для показа перед запуском операции (5-7 строк)."""
    risk_level = prediction.get("risk_level", "low")
    success_prob = prediction.get("success_probability", 0.7)
    expected_errors = prediction.get("expected_errors", 0)
    duration = prediction.get("estimated_duration_minutes")
    recommended_accs = prediction.get("recommended_accounts", 1)
    warnings = prediction.get("warnings") or []
    risk_reasons = prediction.get("risk_reasons") or []

    _RISK_ICONS = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }

    risk_icon = _RISK_ICONS.get(risk_level, "⚪")
    risk_label_map = {
        "low": "Низкий",
        "medium": "Умеренный",
        "high": "Высокий",
        "critical": "Критический",
    }
    risk_label = risk_label_map.get(risk_level, risk_level)

    lines = ["🤖 <b>Copilot прогноз</b>"]
    lines.append(
        f"{risk_icon} Риск: <b>{risk_label}</b> | Успех: <b>{round(success_prob * 100)}%</b>"
    )

    if duration is not None:
        if duration >= 60:
            dur_str = f"{int(duration // 60)}ч {int(duration % 60)}мин"
        else:
            dur_str = f"~{int(duration)} мин"
        lines.append(f"⏱ Ожидаемое время: <b>{dur_str}</b>")

    if expected_errors > 0:
        lines.append(f"⚡ Ожидаемые ошибки: ~{int(expected_errors)} шт.")

    lines.append(f"📱 Рекомендуется аккаунтов: <b>{recommended_accs}</b>")

    # Критические инсайты (не более 2)
    critical = [i for i in insights if i.severity == "critical"][:2]
    for ci in critical:
        lines.append(f"🚨 {html.escape(ci.title)}")

    # Предупреждения из прогноза
    for w in warnings[:2]:
        lines.append(f"⚠️ {html.escape(w)}")

    # Причины риска если есть
    if risk_reasons and risk_level in ("high", "critical"):
        reason = html.escape(risk_reasons[0])
        lines.append(f"📌 {reason}")

    return "\n".join(lines)


async def run_copilot_loop(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: каждые 30 минут анализирует инфраструктуру всех пользователей.

    При обнаружении critical-проблем — уведомляет владельцев через notify_if_enabled.
    """
    import asyncio
    from database import db as _db

    log.info("infra_copilot: background loop started (interval=30min)")
    await asyncio.sleep(300)  # начальная задержка 5 минут после старта бота

    while True:
        try:
            await reload_snoozes_from_db(pool)
            # Получить список активных владельцев с аккаунтами
            owner_ids = await pool.fetch(
                """SELECT DISTINCT owner_id FROM tg_accounts
                   WHERE is_active=TRUE
                   GROUP BY owner_id HAVING COUNT(*) > 0""",
            )
            for row in owner_ids:
                owner_id = row["owner_id"]
                try:
                    if is_snoozed(owner_id):
                        log.debug("infra_copilot: owner=%d alerts snoozed", owner_id)
                        await asyncio.sleep(1)
                        continue
                    insights = await run_full_analysis(
                        pool, owner_id, include_predictions=False
                    )
                    critical = [i for i in insights if i.severity == "critical"]
                    if critical:
                        report = _format_critical_alert(critical[:3])
                        markup = _snooze_markup()
                        await _db.notify_if_enabled(
                            pool,
                            bot,
                            owner_id,
                            "restriction",
                            report,
                            reply_markup=markup,
                        )
                    await asyncio.sleep(1)  # небольшая пауза между пользователями
                except Exception as e:
                    log.debug("infra_copilot loop owner=%d: %s", owner_id, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("infra_copilot loop error: %s", e)

        await asyncio.sleep(1800)  # 30 минут


def _format_critical_alert(insights: list[CopilotInsight]) -> str:
    lines = ["🚨 <b>Infrastructure Copilot: критические проблемы</b>\n"]
    for i in insights:
        lines.append(f"• <b>{html.escape(i.title)}</b>")
        lines.append(f"  {html.escape(i.explanation)}")
        if i.recommendation:
            lines.append(f"  💡 {html.escape(i.recommendation)}")
    lines.append("\n<i>Отложить уведомления:</i>")
    return "\n".join(lines)


def _snooze_markup():
    """Inline-клавиатура для снуза уведомлений Copilot."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import InfraCb

    kb = InlineKeyboardBuilder()
    kb.button(text="😴 1ч", callback_data=InfraCb(action="snooze", page=1))
    kb.button(text="😴 6ч", callback_data=InfraCb(action="snooze", page=6))
    kb.button(text="😴 24ч", callback_data=InfraCb(action="snooze", page=24))
    kb.button(text="🔍 Copilot", callback_data=InfraCb(action="copilot"))
    kb.adjust(3, 1)
    return kb.as_markup()
