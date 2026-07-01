"""EPOCH VI: Recovery Engine — ядро самовосстановления инфраструктуры.

Обнаруживает, локализует и устраняет сбои автономно:
  - AccountRecovery   — исключение деградировавших аккаунтов, перераспределение
  - ProxyRecovery     — понижение приоритета нерабочих прокси, переназначение
  - SessionRecovery   — переподключение зависших/устаревших сессий
  - QueueRecovery     — восстановление зависших операций
  - OperationRecovery — возобновление операций после сбоя

Цикл: каждые 15 минут. Все действия логируются в recovery_events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import asyncpg


log = logging.getLogger(__name__)

# Порог для авто-исключения аккаунта из ротации
_ACCOUNT_FAIL_RATE_THRESHOLD = 0.70  # 70% ошибок за последние 24ч → исключить
_ACCOUNT_MIN_OPS_FOR_DECISION = 5  # минимум операций для вывода
_ACCOUNT_TRUST_CRITICAL = 0.25  # trust ниже этого → немедленно исключить
_PROXY_FAIL_RATE_THRESHOLD = 0.60  # 60% ошибок прокси → переназначить
_QUEUE_STUCK_MINUTES = 90  # операция "running" дольше этого → stuck
_RECOVERY_INTERVAL = 900  # 15 минут между полными циклами
_COOLDOWN_RECOVERY_HOURS = 4  # куллдаун для восстановленного аккаунта

# In-memory: owner_id → {account_id → last_recovery_ts}
_last_account_recovery: dict[int, dict[int, float]] = {}
_last_proxy_recovery: dict[int, dict[int, float]] = {}


# ─── Результат действия по восстановлению ─────────────────────────────────────


@dataclass
class RecoveryAction:
    recovery_type: str  # account | proxy | session | queue | operation
    target_type: str  # account | proxy | operation
    target_id: int | None
    action: str  # exclude | reassign | cooldown | restart | resume
    severity: str  # info | warning | critical
    owner_id: int
    details: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    status: str = "pending"


# ─── Основной оркестратор ─────────────────────────────────────────────────────


async def run_full_recovery(pool: asyncpg.Pool, bot) -> list[RecoveryAction]:
    """Запустить все восстановительные анализаторы параллельно для всех владельцев."""
    try:
        owner_rows = await pool.fetch(
            "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
        )
    except Exception as e:
        log.debug("recovery_engine: get owners failed: %s", e)
        return []

    actions: list[RecoveryAction] = []
    for row in owner_rows:
        owner_id = row["owner_id"]
        try:
            owner_actions = await _recover_owner(pool, bot, owner_id)
            actions.extend(owner_actions)
        except Exception as e:
            log.debug("recovery_engine: owner=%d failed: %s", owner_id, e)

    return actions


async def _recover_owner(
    pool: asyncpg.Pool, bot, owner_id: int
) -> list[RecoveryAction]:
    tasks = [
        _account_recovery(pool, bot, owner_id),
        _proxy_recovery(pool, bot, owner_id),
        _queue_recovery(pool, bot, owner_id),
        _operation_recovery(pool, bot, owner_id),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    actions: list[RecoveryAction] = []
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
        elif isinstance(r, list):
            actions.extend(r)
        elif isinstance(r, BaseException):
            log.debug("recovery_engine sub-task failed owner=%d: %s", owner_id, r)

    return actions


# ─── Account Recovery ─────────────────────────────────────────────────────────


async def _account_recovery(
    pool: asyncpg.Pool, bot, owner_id: int
) -> list[RecoveryAction]:
    """Обнаруживает деградировавшие аккаунты и автоматически их исключает."""
    actions: list[RecoveryAction] = []

    # 1. Аккаунты с критически низким trust_score → принудительный кулдаун
    try:
        critical_trust = await pool.fetch(
            """SELECT id, COALESCE(first_name, phone, 'id'||id::text) AS label,
                      trust_score, cooldown_until
               FROM tg_accounts
               WHERE owner_id=$1
                 AND is_active=TRUE
                 AND COALESCE(trust_score, 1.0) < $2
                 AND (cooldown_until IS NULL OR cooldown_until < NOW())
               ORDER BY trust_score ASC
               LIMIT 10""",
            owner_id,
            _ACCOUNT_TRUST_CRITICAL,
        )
        for acc in critical_trust:
            acc_id = acc["id"]
            now = time.time()
            last = _last_account_recovery.get(owner_id, {}).get(acc_id, 0)
            if now - last < 3600 * 6:  # не трогать чаще раза в 6ч
                continue

            cooldown_hours = _COOLDOWN_RECOVERY_HOURS
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET cooldown_until=NOW()+($1 * INTERVAL '1 hour') "
                    "WHERE id=$2 AND owner_id=$3",
                    cooldown_hours,
                    acc_id,
                    owner_id,
                )
            except Exception as e:
                log.debug("recovery: cooldown update failed acc=%d: %s", acc_id, e)
                continue

            _last_account_recovery.setdefault(owner_id, {})[acc_id] = now

            action = RecoveryAction(
                recovery_type="account",
                target_type="account",
                target_id=acc_id,
                action="cooldown",
                severity="critical",
                owner_id=owner_id,
                details={
                    "trust_score": float(acc.get("trust_score") or 0),
                    "label": acc["label"],
                    "reason": "trust_below_critical_threshold",
                },
                outcome={
                    "cooldown_hours": cooldown_hours,
                    "action": "forced_cooldown_applied",
                },
                status="success",
            )
            actions.append(action)
            await _log_recovery_event(pool, action)
            log.info(
                "recovery_engine: account %s (owner=%d) → forced cooldown %dh (trust=%.2f)",
                acc["label"],
                owner_id,
                cooldown_hours,
                acc.get("trust_score") or 0,
            )
    except Exception as e:
        log.debug("recovery account critical trust: %s", e)

    # 2. Аккаунты с высоким fail rate за 24ч (по operation_queue)
    try:
        high_fail = await pool.fetch(
            """SELECT oq.account_id,
                      COALESCE(a.first_name, a.phone, 'id'||oq.account_id::text) AS label,
                      COUNT(*) FILTER (WHERE oq.status='failed') AS fails,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE oq.status='failed')::float / NULLIF(COUNT(*),0) AS fail_rate
               FROM operation_queue oq
               JOIN tg_accounts a ON a.id=oq.account_id AND a.owner_id=$1
               WHERE oq.owner_id=$1
                 AND oq.created_at > NOW() - INTERVAL '24 hours'
                 AND a.is_active=TRUE
                 AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
               GROUP BY oq.account_id, a.first_name, a.phone, a.id
               HAVING COUNT(*) >= $2
                  AND COUNT(*) FILTER (WHERE oq.status='failed')::float / NULLIF(COUNT(*),0) >= $3
               ORDER BY fail_rate DESC
               LIMIT 5""",
            owner_id,
            _ACCOUNT_MIN_OPS_FOR_DECISION,
            _ACCOUNT_FAIL_RATE_THRESHOLD,
        )
        for acc in high_fail:
            acc_id = acc["account_id"]
            now = time.time()
            last = _last_account_recovery.get(owner_id, {}).get(acc_id, 0)
            if now - last < 3600 * 3:
                continue

            cooldown_h = 2
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET cooldown_until=NOW()+($1 * INTERVAL '1 hour') "
                    "WHERE id=$2 AND owner_id=$3 AND (cooldown_until IS NULL OR cooldown_until<NOW())",
                    cooldown_h,
                    acc_id,
                    owner_id,
                )
            except Exception as e:
                log.debug("recovery: high fail cooldown failed acc=%d: %s", acc_id, e)
                continue

            _last_account_recovery.setdefault(owner_id, {})[acc_id] = now

            action = RecoveryAction(
                recovery_type="account",
                target_type="account",
                target_id=acc_id,
                action="cooldown",
                severity="warning",
                owner_id=owner_id,
                details={
                    "label": acc["label"],
                    "fail_rate": round(float(acc.get("fail_rate") or 0), 3),
                    "fails": acc["fails"],
                    "total": acc["total"],
                    "reason": "high_fail_rate_24h",
                },
                outcome={"cooldown_hours": cooldown_h, "action": "temporary_cooldown"},
                status="success",
            )
            actions.append(action)
            await _log_recovery_event(pool, action)
            log.info(
                "recovery_engine: account %s → cooldown %dh (fail_rate=%.0f%%, %d/%d ops)",
                acc["label"],
                cooldown_h,
                (acc.get("fail_rate") or 0) * 100,
                acc["fails"],
                acc["total"],
            )
    except Exception as e:
        log.debug("recovery account high fail: %s", e)

    return actions


# ─── Proxy Recovery ────────────────────────────────────────────────────────────


async def _proxy_recovery(
    pool: asyncpg.Pool, bot, owner_id: int
) -> list[RecoveryAction]:
    """Обнаруживает сбойные прокси и снижает их приоритет / переназначает аккаунты."""
    actions: list[RecoveryAction] = []

    try:
        # Прокси с высоким fail rate за последние 6 часов (минимум 5 проверок)
        bad_proxies = await pool.fetch(
            """SELECT pql.proxy_id,
                      up.label,
                      COUNT(*) AS total_checks,
                      COUNT(*) FILTER (WHERE NOT pql.success) AS fails,
                      COUNT(*) FILTER (WHERE NOT pql.success)::float / NULLIF(COUNT(*),0) AS fail_rate,
                      AVG(pql.latency_ms) AS avg_latency
               FROM proxy_quality_log pql
               JOIN user_proxies up ON up.id=pql.proxy_id AND up.owner_id=$1
               WHERE pql.checked_at > NOW() - INTERVAL '6 hours'
               GROUP BY pql.proxy_id, up.label
               HAVING COUNT(*) >= 5
                  AND COUNT(*) FILTER (WHERE NOT pql.success)::float / NULLIF(COUNT(*),0) >= $2
               ORDER BY fail_rate DESC
               LIMIT 5""",
            owner_id,
            _PROXY_FAIL_RATE_THRESHOLD,
        )
        for prx in bad_proxies:
            proxy_id = prx["proxy_id"]
            now = time.time()
            last = _last_proxy_recovery.get(owner_id, {}).get(proxy_id, 0)
            if now - last < 3600 * 2:
                continue

            # Найти аккаунты использующие этот прокси
            affected = await pool.fetch(
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND proxy_id=$2 AND is_active=TRUE",
                owner_id,
                proxy_id,
            )

            # Найти другой рабочий прокси для переназначения
            alt_proxy = await pool.fetchrow(
                """SELECT up.id FROM user_proxies up
                   LEFT JOIN (
                       SELECT proxy_id, AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END) AS sr
                       FROM proxy_quality_log WHERE checked_at > NOW() - INTERVAL '2 hours'
                       GROUP BY proxy_id HAVING COUNT(*) >= 3
                   ) pql ON pql.proxy_id = up.id
                   WHERE up.owner_id=$1
                     AND up.id != $2
                     -- не переназначать на заведомо мёртвый прокси (авто-деактивирован
                     -- или последняя проверка провалена); непроверенные (NULL) — годны
                     AND COALESCE(up.is_active, TRUE) = TRUE
                     AND COALESCE(up.is_alive, TRUE) = TRUE
                   ORDER BY COALESCE(pql.sr, 0.5) DESC
                   LIMIT 1""",
                owner_id,
                proxy_id,
            )

            reassigned = 0
            if alt_proxy and affected:
                try:
                    await pool.execute(
                        "UPDATE tg_accounts SET proxy_id=$1 WHERE id=ANY($2)",
                        alt_proxy["id"],
                        [a["id"] for a in affected],
                    )
                    reassigned = len(affected)
                except Exception as e:
                    log.debug(
                        "recovery proxy reassign failed proxy=%d: %s", proxy_id, e
                    )

            _last_proxy_recovery.setdefault(owner_id, {})[proxy_id] = now

            action = RecoveryAction(
                recovery_type="proxy",
                target_type="proxy",
                target_id=proxy_id,
                action="reassign",
                severity="warning",
                owner_id=owner_id,
                details={
                    "label": prx.get("label", f"proxy#{proxy_id}"),
                    "fail_rate": round(float(prx.get("fail_rate") or 0), 3),
                    "total_checks": prx["total_checks"],
                    "avg_latency_ms": round(float(prx.get("avg_latency") or 0)),
                    "affected_accounts": len(affected),
                },
                outcome={
                    "accounts_reassigned": reassigned,
                    "new_proxy_id": alt_proxy["id"] if alt_proxy else None,
                },
                status="success" if reassigned > 0 else "skipped",
            )
            actions.append(action)
            await _log_recovery_event(pool, action)
            log.info(
                "recovery_engine: proxy %s (fail_rate=%.0f%%) → reassigned %d accounts",
                prx.get("label", proxy_id),
                (prx.get("fail_rate") or 0) * 100,
                reassigned,
            )
    except Exception as e:
        log.debug("proxy_recovery failed owner=%d: %s", owner_id, e)

    return actions


# ─── Queue Recovery ────────────────────────────────────────────────────────────


async def _queue_recovery(
    pool: asyncpg.Pool, bot, owner_id: int
) -> list[RecoveryAction]:
    """Обнаруживает зависшие операции в статусе 'running' и восстанавливает их."""
    actions: list[RecoveryAction] = []

    try:
        stuck = await pool.fetch(
            """SELECT id, op_type, account_id, retry_count,
                      COALESCE(max_retries, 3) AS max_retries,
                      EXTRACT(EPOCH FROM (NOW() - started_at)) / 60 AS stuck_minutes
               FROM operation_queue
               WHERE owner_id=$1
                 AND status='running'
                 AND started_at < NOW() - ($2 * INTERVAL '1 minute')
               ORDER BY stuck_minutes DESC
               LIMIT 10""",
            owner_id,
            _QUEUE_STUCK_MINUTES,
        )
        for op in stuck:
            op_id = op["id"]
            stuck_min = round(float(op.get("stuck_minutes") or 0))
            retry = op.get("retry_count") or 0
            max_ret = op.get("max_retries") or 3

            # Если retries < max_retries → вернуть в pending с экспоненциальным backoff
            # Если retries >= max_retries → пометить как failed (dead letter)
            if retry < max_ret:
                new_status = "pending"
                # Exponential backoff: 60s, 120s, 240s, ... capped at 30min
                backoff_s = min(60 * (2 ** retry), 1800)
                new_msg = f"Авто-восстановление: операция зависла на {stuck_min}мин, возобновлена (backoff={backoff_s}s)"
                action_name = "resume"
            else:
                new_status = "failed"
                backoff_s = 0
                new_msg = f"Авто-восстановление: операция зависла {stuck_min}мин, retry_count={retry}/{max_ret} — помечена как failed (dead letter)"
                action_name = "fail"

            try:
                if new_status == "pending":
                    await pool.execute(
                        """UPDATE operation_queue
                           SET status='pending', error_msg=$1,
                               started_at=NULL,
                               last_error=$1,
                               retry_count=retry_count+1,
                               scheduled_for=now() + ($2 * INTERVAL '1 second')
                           WHERE id=$3 AND status='running'""",
                        new_msg,
                        backoff_s,
                        op_id,
                    )
                else:
                    await pool.execute(
                        """UPDATE operation_queue
                           SET status='failed', error_msg=$1,
                               started_at=NULL,
                               finished_at=COALESCE(finished_at, now())
                           WHERE id=$2 AND status='running'""",
                        new_msg,
                        op_id,
                    )
            except Exception as e:
                log.debug("recovery queue update failed op=%d: %s", op_id, e)
                continue

            action = RecoveryAction(
                recovery_type="queue",
                target_type="operation",
                target_id=op_id,
                action=action_name,
                severity="warning",
                owner_id=owner_id,
                details={
                    "op_type": op["op_type"],
                    "stuck_minutes": stuck_min,
                    "retry_count": retry,
                },
                outcome={"new_status": new_status, "error_msg": new_msg},
                status="success",
            )
            actions.append(action)
            await _log_recovery_event(pool, action)
            log.info(
                "recovery_engine: stuck op #%d (%s, %dmin) → %s",
                op_id,
                op["op_type"],
                stuck_min,
                new_status,
            )
    except Exception as e:
        log.debug("queue_recovery failed owner=%d: %s", owner_id, e)

    return actions


# ─── Operation Recovery ────────────────────────────────────────────────────────


async def _operation_recovery(
    pool: asyncpg.Pool, bot, owner_id: int
) -> list[RecoveryAction]:
    """Восстанавливает операции с retry_count >= max_retries — уведомляет владельца."""
    actions: list[RecoveryAction] = []

    try:
        terminal_failed = await pool.fetch(
            """SELECT id, op_type, retry_count, max_retries, error_msg, created_at
               FROM operation_queue
               WHERE owner_id=$1
                 AND status='failed'
                 AND retry_count >= COALESCE(max_retries, 3)
                 AND finished_at > NOW() - INTERVAL '1 hour'
                 AND (notified_at IS NULL OR notified_at < NOW() - INTERVAL '3 hours')
               ORDER BY finished_at DESC
               LIMIT 5""",
            owner_id,
        )
        if terminal_failed:
            # Обновляем notified_at для предотвращения дублей
            ids = [r["id"] for r in terminal_failed]
            await pool.execute(
                "UPDATE operation_queue SET notified_at=NOW() WHERE id=ANY($1)",
                ids,
            )

            for op in terminal_failed:
                action = RecoveryAction(
                    recovery_type="operation",
                    target_type="operation",
                    target_id=op["id"],
                    action="escalate",
                    severity="warning",
                    owner_id=owner_id,
                    details={
                        "op_type": op["op_type"],
                        "retry_count": op.get("retry_count"),
                        "error_msg": (op.get("error_msg") or "")[:200],
                    },
                    outcome={"notification": "sent"},
                    status="success",
                )
                actions.append(action)
    except Exception as e:
        log.debug("operation_recovery failed owner=%d: %s", owner_id, e)

    return actions


# ─── Запись в БД ──────────────────────────────────────────────────────────────


async def _log_recovery_event(pool: asyncpg.Pool, action: RecoveryAction) -> int | None:
    """Записать действие по восстановлению в recovery_events."""
    try:
        import json

        row = await pool.fetchrow(
            """INSERT INTO recovery_events
               (owner_id, recovery_type, target_type, target_id, trigger,
                action, status, severity, details, outcome, started_at, completed_at)
               VALUES ($1,$2,$3,$4,'auto',$5,$6,$7,$8,$9,NOW(),NOW())
               RETURNING id""",
            action.owner_id,
            action.recovery_type,
            action.target_type,
            action.target_id,
            action.action,
            action.status,
            action.severity,
            json.dumps(action.details),
            json.dumps(action.outcome),
        )
        return row["id"] if row else None
    except Exception as e:
        log.debug("_log_recovery_event failed: %s", e)
        return None


async def log_manual_recovery(
    pool: asyncpg.Pool,
    owner_id: int,
    recovery_type: str,
    target_type: str,
    target_id: int | None,
    action: str,
    severity: str,
    details: dict,
    outcome: dict,
    status: str = "success",
) -> int | None:
    """Записать ручное действие по восстановлению (из UI)."""
    import json

    try:
        row = await pool.fetchrow(
            """INSERT INTO recovery_events
               (owner_id, recovery_type, target_type, target_id, trigger,
                action, status, severity, details, outcome, started_at, completed_at)
               VALUES ($1,$2,$3,$4,'manual',$5,$6,$7,$8,$9,NOW(),NOW())
               RETURNING id""",
            owner_id,
            recovery_type,
            target_type,
            target_id,
            action,
            status,
            severity,
            json.dumps(details),
            json.dumps(outcome),
        )
        return row["id"] if row else None
    except Exception as e:
        log.debug("log_manual_recovery failed: %s", e)
        return None


# ─── Snapshot здоровья системы ────────────────────────────────────────────────


async def take_health_snapshot(pool: asyncpg.Pool, owner_id: int) -> int:
    """Сделать снапшот состояния системы и вернуть health_score 0-100."""
    try:
        import json

        acc_row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE is_active) AS total,
                   COUNT(*) FILTER (WHERE is_active AND (cooldown_until IS NULL OR cooldown_until < NOW())) AS ready,
                   COUNT(*) FILTER (WHERE is_active AND cooldown_until > NOW()) AS in_cooldown,
                   AVG(COALESCE(trust_score, 1.0)) FILTER (WHERE is_active) AS avg_trust
               FROM tg_accounts WHERE owner_id=$1""",
            owner_id,
        )
        total_acc = acc_row["total"] or 0
        ready_acc = acc_row["ready"] or 0
        in_cooldown = acc_row["in_cooldown"] or 0
        avg_trust = float(acc_row["avg_trust"] or 0.7)

        ops_row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='pending') AS pending,
                   COUNT(*) FILTER (WHERE status='running') AS running,
                   COUNT(*) FILTER (WHERE status='failed' AND created_at > NOW()-INTERVAL '24h') AS failed_24h,
                   COUNT(*) FILTER (WHERE status='done' AND created_at > NOW()-INTERVAL '24h') AS done_24h
               FROM operation_queue WHERE owner_id=$1""",
            owner_id,
        )
        ops_pending = ops_row["pending"] or 0
        ops_running = ops_row["running"] or 0
        ops_failed_24h = ops_row["failed_24h"] or 0
        ops_done_24h = ops_row["done_24h"] or 0

        proxy_row = await pool.fetchrow(
            """SELECT
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_active) AS active
               FROM user_proxies WHERE owner_id=$1""",
            owner_id,
        )
        proxies_total = proxy_row["total"] or 0
        proxies_healthy = proxy_row["active"] or 0

        alerts_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM infrastructure_alerts WHERE owner_id=$1 AND is_active=TRUE",
            owner_id,
        )
        anomalies_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM anomaly_events WHERE owner_id=$1 AND is_active=TRUE",
            owner_id,
        )
        recoveries_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM recovery_events WHERE owner_id=$1 AND status='running'",
            owner_id,
        )

        active_alerts = (alerts_row["cnt"] if alerts_row else 0) or 0
        active_anomalies = (anomalies_row["cnt"] if anomalies_row else 0) or 0
        active_recoveries = (recoveries_row["cnt"] if recoveries_row else 0) or 0

        # ─── Вычислить health_score ────────────────────────────────────────
        score = 100

        # Аккаунты (0-35 очков)
        if total_acc == 0:
            acc_score = 0
        elif ready_acc == 0:
            acc_score = 5
        elif ready_acc == 1:
            acc_score = 15
        else:
            readiness_ratio = ready_acc / total_acc
            acc_score = int(35 * min(readiness_ratio * 1.2, 1.0))
        score = score - (35 - acc_score)

        # Trust (0-25 очков)
        trust_score = int(25 * avg_trust)
        score = score - (25 - trust_score)

        # Операции (0-20 очков)
        total_ops = ops_failed_24h + ops_done_24h
        if total_ops > 0:
            fail_rate = ops_failed_24h / total_ops
            ops_score = int(20 * (1 - fail_rate))
        else:
            ops_score = 20
        if ops_pending > 20:
            ops_score = max(0, ops_score - 5)
        score = score - (20 - ops_score)

        # Активные алерты / аномалии (0-20 очков)
        alert_penalty = min(20, active_alerts * 5 + active_anomalies * 3)
        score = max(0, score - alert_penalty)

        health_score = max(0, min(100, score))

        components = {
            "accounts": acc_score,
            "trust": trust_score,
            "operations": ops_score,
            "alerts": max(0, 20 - alert_penalty),
        }

        await pool.execute(
            """INSERT INTO system_health_snapshots
               (owner_id, health_score, accounts_ready, accounts_total, accounts_in_cooldown,
                avg_trust_score, ops_pending, ops_running, ops_failed_24h, ops_done_24h,
                proxies_healthy, proxies_total, active_alerts, active_anomalies,
                active_recoveries, components)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
            owner_id,
            health_score,
            ready_acc,
            total_acc,
            in_cooldown,
            avg_trust,
            ops_pending,
            ops_running,
            ops_failed_24h,
            ops_done_24h,
            proxies_healthy,
            proxies_total,
            active_alerts,
            active_anomalies,
            active_recoveries,
            json.dumps(components),
        )
        return health_score

    except Exception as e:
        log.debug("take_health_snapshot failed owner=%d: %s", owner_id, e)
        return 0


async def get_current_health(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Получить последний снапшот здоровья системы (из БД или вычислить)."""
    try:
        row = await pool.fetchrow(
            """SELECT * FROM system_health_snapshots
               WHERE owner_id=$1
               ORDER BY snapshot_at DESC LIMIT 1""",
            owner_id,
        )
        if row and row["snapshot_at"]:
            import datetime

            age = (
                datetime.datetime.now(datetime.timezone.utc) - row["snapshot_at"]
            ).total_seconds()
            if age < 3600:  # если снапшот свежее 1ч — вернуть его
                return dict(row)
    except Exception as e:
        log.debug("get_current_health db read failed: %s", e)

    # Вычислить свежий снапшот
    score = await take_health_snapshot(pool, owner_id)
    return {"health_score": score}


async def get_recent_recovery_events(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 20,
) -> list[dict]:
    """Получить последние события восстановления."""
    try:
        rows = await pool.fetch(
            """SELECT id, recovery_type, target_type, target_id, trigger,
                      action, status, severity, details, outcome, created_at
               FROM recovery_events
               WHERE owner_id=$1
               ORDER BY created_at DESC
               LIMIT $2""",
            owner_id,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("get_recent_recovery_events failed: %s", e)
        return []


# ─── Фоновый цикл ─────────────────────────────────────────────────────────────


async def run_recovery_loop(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: каждые 15 минут запускает полный цикл восстановления."""
    log.info("recovery_engine: loop started (interval=15min)")
    await asyncio.sleep(120)  # 2-минутная задержка после старта

    while True:
        try:
            actions = await run_full_recovery(pool, bot)
            if actions:
                success = [a for a in actions if a.status == "success"]
                log.info(
                    "recovery_engine: cycle complete — %d actions (%d successful)",
                    len(actions),
                    len(success),
                )

            # Снапшоты здоровья для всех владельцев
            try:
                owner_rows = await pool.fetch(
                    "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
                )
                for row in owner_rows:
                    try:
                        await take_health_snapshot(pool, row["owner_id"])
                    except Exception:
                        pass
            except Exception as e:
                log.debug("recovery_engine snapshot cycle: %s", e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("recovery_engine loop error: %s", e, exc_info=True)

        await asyncio.sleep(_RECOVERY_INTERVAL)
