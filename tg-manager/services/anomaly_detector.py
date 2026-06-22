"""EPOCH VI: Anomaly Detector — проактивное обнаружение аномалий инфраструктуры.

Детектирует отклонения ДО того, как они становятся катастрофой:
  - Резкий рост ошибок (error_spike)
  - Падение success_rate ниже базовой линии (success_drop)
  - Взрывной рост очереди (queue_surge)
  - Волна флуд-блокировок (flood_wave)
  - Коллапс trust score (trust_collapse)
  - Деградация латентности прокси (latency_spike)

Цикл: каждые 5 минут. Аномалии записываются в anomaly_events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_ANOMALY_INTERVAL = 300  # 5 минут между циклами
_ERROR_SPIKE_MULTIPLIER = 2.5  # текущий fail_rate > baseline * 2.5 → spike
_SUCCESS_DROP_THRESHOLD = 0.25  # падение success_rate на 25% → аномалия
_QUEUE_SURGE_MULTIPLIER = 3.0  # очередь выросла в 3x → surge
_FLOOD_WAVE_THRESHOLD = 5  # 5+ flood событий за 1ч → wave
_LATENCY_SPIKE_MS = 3000  # средняя латентность > 3с → spike
_TRUST_COLLAPSE_DROP = 0.20  # trust упал на 20% за 2ч → collapse

# In-memory baseline per owner (обновляются каждый цикл)
_baseline_cache: dict[int, dict[str, Any]] = {}
_last_anomaly_seen: dict[str, float] = {}  # anomaly_type:owner_id → last_ts


@dataclass
class Anomaly:
    anomaly_type: str  # error_spike | success_drop | queue_surge | flood_wave | trust_collapse | latency_spike
    detector: str  # account | proxy | queue | timing | flood
    severity: str  # info | warning | critical
    title: str
    description: str
    owner_id: int
    baseline_value: float = 0.0
    anomaly_value: float = 0.0
    deviation_pct: float = 0.0
    affected_count: int = 0
    target_ids: list[int] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# ─── Главная точка входа ──────────────────────────────────────────────────────


async def run_full_detection(pool: asyncpg.Pool, bot) -> list[Anomaly]:
    """Запустить все детекторы для всех владельцев."""
    try:
        owner_rows = await pool.fetch(
            "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
        )
    except Exception as e:
        log.debug("anomaly_detector: get owners failed: %s", e)
        return []

    all_anomalies: list[Anomaly] = []
    for row in owner_rows:
        owner_id = row["owner_id"]
        try:
            anomalies = await _detect_owner(pool, owner_id)
            for a in anomalies:
                if await _should_record(pool, a):
                    await _record_anomaly(pool, a)
                    all_anomalies.append(a)
                    if a.severity == "critical":
                        await _notify_anomaly(pool, bot, a)
        except Exception as e:
            log.debug("anomaly_detector: owner=%d failed: %s", owner_id, e)

    return all_anomalies


async def _detect_owner(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    tasks = [
        _detect_error_spike(pool, owner_id),
        _detect_queue_surge(pool, owner_id),
        _detect_flood_wave(pool, owner_id),
        _detect_trust_collapse(pool, owner_id),
        _detect_latency_spike(pool, owner_id),
        _detect_success_drop(pool, owner_id),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    anomalies: list[Anomaly] = []
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, list):
            anomalies.extend(r)
    return anomalies


# ─── Error Spike ──────────────────────────────────────────────────────────────


async def _detect_error_spike(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Резкий рост ошибок операций по сравнению с базовой линией 24ч."""
    anomalies: list[Anomaly] = []
    try:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='failed' AND created_at > NOW()-INTERVAL '1 hour') AS fails_1h,
                   COUNT(*) FILTER (WHERE status IN ('done','failed') AND created_at > NOW()-INTERVAL '1 hour') AS total_1h,
                   COUNT(*) FILTER (WHERE status='failed' AND created_at BETWEEN NOW()-INTERVAL '25 hours' AND NOW()-INTERVAL '1 hour') AS fails_24h,
                   COUNT(*) FILTER (WHERE status IN ('done','failed') AND created_at BETWEEN NOW()-INTERVAL '25 hours' AND NOW()-INTERVAL '1 hour') AS total_24h
               FROM operation_queue WHERE owner_id=$1""",
            owner_id,
        )
        if not row:
            return anomalies

        fails_1h = row["fails_1h"] or 0
        total_1h = row["total_1h"] or 0
        fails_24h = row["fails_24h"] or 0
        total_24h = row["total_24h"] or 0

        if total_1h < 3 or total_24h < 5:
            return anomalies

        rate_1h = fails_1h / total_1h
        baseline_rate = fails_24h / total_24h

        if baseline_rate < 0.05:
            baseline_rate = 0.10  # минимальный порог

        if rate_1h >= baseline_rate * _ERROR_SPIKE_MULTIPLIER and rate_1h > 0.3:
            dev = round((rate_1h - baseline_rate) / baseline_rate * 100)
            severity = "critical" if rate_1h > 0.6 else "warning"
            anomalies.append(
                Anomaly(
                    anomaly_type="error_spike",
                    detector="queue",
                    severity=severity,
                    title=f"Всплеск ошибок: {int(rate_1h * 100)}% за последний час",
                    description=(
                        f"Операций с ошибками за последний час: {int(rate_1h * 100)}% ({fails_1h}/{total_1h}). "
                        f"Базовая линия (24ч): {int(baseline_rate * 100)}%. "
                        f"Рост: +{dev}%."
                    ),
                    owner_id=owner_id,
                    baseline_value=round(baseline_rate, 3),
                    anomaly_value=round(rate_1h, 3),
                    deviation_pct=dev,
                    affected_count=fails_1h,
                )
            )
    except Exception as e:
        log.debug("_detect_error_spike owner=%d: %s", owner_id, e)
    return anomalies


# ─── Success Drop ─────────────────────────────────────────────────────────────


async def _detect_success_drop(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Падение success rate операций по типам."""
    anomalies: list[Anomaly] = []
    try:
        rows = await pool.fetch(
            """SELECT op_type,
                   COUNT(*) FILTER (WHERE status='done' AND created_at > NOW()-INTERVAL '3 hours') AS done_3h,
                   COUNT(*) FILTER (WHERE status IN ('done','failed') AND created_at > NOW()-INTERVAL '3 hours') AS total_3h,
                   COUNT(*) FILTER (WHERE status='done' AND created_at BETWEEN NOW()-INTERVAL '27 hours' AND NOW()-INTERVAL '3 hours') AS done_24h,
                   COUNT(*) FILTER (WHERE status IN ('done','failed') AND created_at BETWEEN NOW()-INTERVAL '27 hours' AND NOW()-INTERVAL '3 hours') AS total_24h
               FROM operation_queue
               WHERE owner_id=$1
               GROUP BY op_type
               HAVING COUNT(*) FILTER (WHERE created_at > NOW()-INTERVAL '3 hours') >= 3
                  AND COUNT(*) FILTER (WHERE created_at BETWEEN NOW()-INTERVAL '27 hours' AND NOW()-INTERVAL '3 hours') >= 5""",
            owner_id,
        )
        for row in rows:
            done_3h = row["done_3h"] or 0
            total_3h = row["total_3h"] or 0
            done_24h = row["done_24h"] or 0
            total_24h = row["total_24h"] or 0

            if total_3h == 0 or total_24h == 0:
                continue

            rate_3h = done_3h / total_3h
            baseline = done_24h / total_24h

            if baseline - rate_3h >= _SUCCESS_DROP_THRESHOLD and baseline > 0.5:
                dev = round((baseline - rate_3h) / baseline * 100)
                anomalies.append(
                    Anomaly(
                        anomaly_type="success_drop",
                        detector="account",
                        severity="warning",
                        title=f"Падение успеха {row['op_type']}: -{dev}%",
                        description=(
                            f"Тип операции '{row['op_type']}': за последние 3ч успех {int(rate_3h * 100)}% "
                            f"против базовой линии {int(baseline * 100)}% за 24ч. "
                            f"Падение: {dev}%."
                        ),
                        owner_id=owner_id,
                        baseline_value=round(baseline, 3),
                        anomaly_value=round(rate_3h, 3),
                        deviation_pct=dev,
                        affected_count=int(total_3h - done_3h),
                        metadata={"op_type": row["op_type"]},
                    )
                )
    except Exception as e:
        log.debug("_detect_success_drop owner=%d: %s", owner_id, e)
    return anomalies


# ─── Queue Surge ──────────────────────────────────────────────────────────────


async def _detect_queue_surge(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Взрывной рост очереди — pending вырос в N раз за последний час."""
    anomalies: list[Anomaly] = []
    try:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='pending' AND created_at > NOW()-INTERVAL '1 hour') AS new_pending,
                   COUNT(*) FILTER (WHERE status='pending' AND created_at BETWEEN NOW()-INTERVAL '25 hours' AND NOW()-INTERVAL '1 hour') / 24.0 AS avg_hourly_pending
               FROM operation_queue WHERE owner_id=$1""",
            owner_id,
        )
        if not row:
            return anomalies

        new_pending = row["new_pending"] or 0
        avg_hourly = float(row["avg_hourly_pending"] or 0)

        if new_pending < 5:
            return anomalies
        if avg_hourly < 1:
            avg_hourly = 1.0

        if new_pending >= avg_hourly * _QUEUE_SURGE_MULTIPLIER:
            dev = round(new_pending / avg_hourly * 100 - 100)
            anomalies.append(
                Anomaly(
                    anomaly_type="queue_surge",
                    detector="queue",
                    severity="warning",
                    title=f"Взрывной рост очереди: +{new_pending} за час",
                    description=(
                        f"За последний час добавлено {new_pending} задач в очередь — "
                        f"это в {round(new_pending / avg_hourly, 1)}x больше нормального темпа ({avg_hourly:.1f}/ч). "
                        f"Возможна перегрузка воркеров."
                    ),
                    owner_id=owner_id,
                    baseline_value=round(avg_hourly, 1),
                    anomaly_value=float(new_pending),
                    deviation_pct=dev,
                    affected_count=new_pending,
                )
            )
    except Exception as e:
        log.debug("_detect_queue_surge owner=%d: %s", owner_id, e)
    return anomalies


# ─── Flood Wave ───────────────────────────────────────────────────────────────


async def _detect_flood_wave(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Волна флуд-блокировок за последний час — признак системной перегрузки."""
    anomalies: list[Anomaly] = []
    try:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE flood_count_7d > 0) AS accounts_with_flood,
                   SUM(COALESCE(flood_count_7d, 0)) AS total_floods,
                   COUNT(*) FILTER (WHERE is_active) AS active_total
               FROM tg_accounts WHERE owner_id=$1""",
            owner_id,
        )
        if not row:
            return anomalies

        total_floods = int(row["total_floods"] or 0)
        accounts_with_flood = int(row["accounts_with_flood"] or 0)
        active_total = int(row["active_total"] or 0)

        if total_floods < _FLOOD_WAVE_THRESHOLD or active_total == 0:
            return anomalies

        flood_ratio = accounts_with_flood / active_total
        if flood_ratio >= 0.5:
            severity = "critical" if flood_ratio >= 0.8 else "warning"
            dev = round(flood_ratio * 100)
            anomalies.append(
                Anomaly(
                    anomaly_type="flood_wave",
                    detector="account",
                    severity=severity,
                    title=f"Волна флудов: {accounts_with_flood}/{active_total} аккаунтов",
                    description=(
                        f"{accounts_with_flood} из {active_total} активных аккаунтов ({dev}%) "
                        f"получили флуд-блокировки за последние 7 дней (всего {total_floods} блокировок). "
                        f"Признак системной перегрузки или скоординированных действий."
                    ),
                    owner_id=owner_id,
                    baseline_value=0.2,
                    anomaly_value=round(flood_ratio, 3),
                    deviation_pct=dev,
                    affected_count=accounts_with_flood,
                )
            )
    except Exception as e:
        log.debug("_detect_flood_wave owner=%d: %s", owner_id, e)
    return anomalies


# ─── Trust Collapse ───────────────────────────────────────────────────────────


async def _detect_trust_collapse(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Резкое падение среднего trust_score системы."""
    anomalies: list[Anomaly] = []
    try:
        # Текущий средний trust
        current = await pool.fetchrow(
            "SELECT AVG(COALESCE(trust_score, 1.0)) AS avg_trust, COUNT(*) AS cnt "
            "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            owner_id,
        )
        if not current or (current["cnt"] or 0) < 2:
            return anomalies

        avg_trust = float(current["avg_trust"] or 0.7)

        # Baseline: снапшот 2ч назад
        old_snap = await pool.fetchrow(
            """SELECT avg_trust_score FROM system_health_snapshots
               WHERE owner_id=$1
                 AND snapshot_at BETWEEN NOW()-INTERVAL '3 hours' AND NOW()-INTERVAL '1 hour'
               ORDER BY snapshot_at DESC LIMIT 1""",
            owner_id,
        )
        if not old_snap:
            return anomalies

        old_trust = float(old_snap["avg_trust_score"] or avg_trust)
        drop = old_trust - avg_trust

        if drop >= _TRUST_COLLAPSE_DROP and old_trust > 0.4:
            dev = round(drop / old_trust * 100)
            anomalies.append(
                Anomaly(
                    anomaly_type="trust_collapse",
                    detector="account",
                    severity="critical" if avg_trust < 0.4 else "warning",
                    title=f"Коллапс trust score: -{dev}% за 2ч",
                    description=(
                        f"Средний trust_score системы упал с {old_trust:.2f} до {avg_trust:.2f} "
                        f"за последние 2 часа (падение {dev}%). "
                        f"Возможная причина: массовые флуды, проблемы с сессиями или бан-волна."
                    ),
                    owner_id=owner_id,
                    baseline_value=round(old_trust, 3),
                    anomaly_value=round(avg_trust, 3),
                    deviation_pct=dev,
                )
            )
    except Exception as e:
        log.debug("_detect_trust_collapse owner=%d: %s", owner_id, e)
    return anomalies


# ─── Latency Spike ────────────────────────────────────────────────────────────


async def _detect_latency_spike(pool: asyncpg.Pool, owner_id: int) -> list[Anomaly]:
    """Резкий рост латентности прокси — признак деградации или блокировки."""
    anomalies: list[Anomaly] = []
    try:
        rows = await pool.fetch(
            """SELECT up.id, up.label,
                      AVG(pql.latency_ms) FILTER (WHERE pql.checked_at > NOW()-INTERVAL '30 minutes') AS recent_avg,
                      AVG(pql.latency_ms) FILTER (WHERE pql.checked_at BETWEEN NOW()-INTERVAL '6 hours' AND NOW()-INTERVAL '30 minutes') AS baseline_avg
               FROM proxy_quality_log pql
               JOIN user_proxies up ON up.id=pql.proxy_id AND up.owner_id=$1
               WHERE pql.success=TRUE AND pql.checked_at > NOW()-INTERVAL '6 hours'
               GROUP BY up.id, up.label
               HAVING COUNT(*) FILTER (WHERE pql.checked_at > NOW()-INTERVAL '30 minutes') >= 2
                  AND COUNT(*) FILTER (WHERE pql.checked_at BETWEEN NOW()-INTERVAL '6 hours' AND NOW()-INTERVAL '30 minutes') >= 5
                  AND AVG(pql.latency_ms) FILTER (WHERE pql.checked_at > NOW()-INTERVAL '30 minutes') > $2""",
            owner_id,
            _LATENCY_SPIKE_MS,
        )
        bad = []
        for row in rows:
            recent = float(row["recent_avg"] or 0)
            baseline = float(row["baseline_avg"] or recent)
            if baseline < 100:
                baseline = recent
            if recent > baseline * 1.5 or recent > _LATENCY_SPIKE_MS:
                bad.append(row)

        if bad:
            labels = ", ".join((r["label"] or f"proxy#{r['id']}") for r in bad[:3])
            worst_ms = round(float(bad[0]["recent_avg"] or 0))
            dev = round(
                (
                    float(bad[0].get("recent_avg") or 0)
                    - float(bad[0].get("baseline_avg") or 1)
                )
                / max(float(bad[0].get("baseline_avg") or 1), 1)
                * 100
            )
            anomalies.append(
                Anomaly(
                    anomaly_type="latency_spike",
                    detector="proxy",
                    severity="warning",
                    title=f"Всплеск латентности: {worst_ms}ms ({len(bad)} прокси)",
                    description=(
                        f"Прокси {labels} показывают среднюю латентность {worst_ms}ms — "
                        f"выше порога {_LATENCY_SPIKE_MS}ms. "
                        f"Это может привести к таймаутам и сбоям операций."
                    ),
                    owner_id=owner_id,
                    baseline_value=float(bad[0].get("baseline_avg") or 0),
                    anomaly_value=float(bad[0].get("recent_avg") or 0),
                    deviation_pct=max(0, dev),
                    affected_count=len(bad),
                    target_ids=[r["id"] for r in bad],
                )
            )
    except Exception as e:
        log.debug("_detect_latency_spike owner=%d: %s", owner_id, e)
    return anomalies


# ─── Запись и уведомление ─────────────────────────────────────────────────────


async def _should_record(pool: asyncpg.Pool, anomaly: Anomaly) -> bool:
    """Проверить, нет ли уже активной аномалии такого типа (дедупликация)."""
    key = f"{anomaly.anomaly_type}:{anomaly.owner_id}"
    now = time.time()
    last = _last_anomaly_seen.get(key, 0)
    # Дедупликация: не записывать одинаковую аномалию чаще раза в 30 минут
    if now - last < 1800:
        return False
    _last_anomaly_seen[key] = now
    return True


async def _record_anomaly(pool: asyncpg.Pool, anomaly: Anomaly) -> None:
    """Записать аномалию в БД."""
    import json

    try:
        await pool.execute(
            """INSERT INTO anomaly_events
               (owner_id, anomaly_type, detector, severity, title, description,
                baseline_value, anomaly_value, deviation_pct, affected_count,
                target_ids, metadata)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            anomaly.owner_id,
            anomaly.anomaly_type,
            anomaly.detector,
            anomaly.severity,
            anomaly.title,
            anomaly.description,
            anomaly.baseline_value,
            anomaly.anomaly_value,
            anomaly.deviation_pct,
            anomaly.affected_count,
            json.dumps(anomaly.target_ids),
            json.dumps(anomaly.metadata),
        )

        # Авто-разрешение старых аномалий того же типа
        await pool.execute(
            """UPDATE anomaly_events
               SET is_active=FALSE, resolved_at=NOW()
               WHERE owner_id=$1
                 AND anomaly_type=$2
                 AND is_active=TRUE
                 AND detected_at < NOW() - INTERVAL '2 hours'""",
            anomaly.owner_id,
            anomaly.anomaly_type,
        )
    except Exception as e:
        log.debug("_record_anomaly failed: %s", e)


async def _notify_anomaly(pool: asyncpg.Pool, bot, anomaly: Anomaly) -> None:
    """Уведомить владельца о критической аномалии."""
    try:
        from database import db as _db

        text = (
            f"🚨 <b>Аномалия обнаружена</b>\n\n"
            f"<b>{anomaly.title}</b>\n"
            f"{anomaly.description}\n\n"
            f"<i>Recovery Engine анализирует ситуацию...</i>"
        )
        await _db.notify_if_enabled(pool, bot, anomaly.owner_id, "restriction", text)
    except Exception as e:
        log.debug("_notify_anomaly failed: %s", e)


async def get_active_anomalies(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Получить список активных аномалий для владельца."""
    try:
        rows = await pool.fetch(
            """SELECT id, anomaly_type, detector, severity, title, description,
                      baseline_value, anomaly_value, deviation_pct, affected_count,
                      triggered_recovery, detected_at
               FROM anomaly_events
               WHERE owner_id=$1 AND is_active=TRUE
               ORDER BY
                   CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                   detected_at DESC
               LIMIT 20""",
            owner_id,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("get_active_anomalies failed: %s", e)
        return []


async def resolve_anomaly(pool: asyncpg.Pool, anomaly_id: int, owner_id: int) -> bool:
    """Вручную разрешить аномалию."""
    try:
        result = await pool.execute(
            "UPDATE anomaly_events SET is_active=FALSE, resolved_at=NOW() "
            "WHERE id=$1 AND owner_id=$2",
            anomaly_id,
            owner_id,
        )
        return result == "UPDATE 1"
    except Exception as e:
        log.debug("resolve_anomaly failed: %s", e)
        return False


# ─── Фоновый цикл ─────────────────────────────────────────────────────────────


async def run_anomaly_loop(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: каждые 5 минут запускает детекторы аномалий."""
    log.info("anomaly_detector: loop started (interval=5min)")
    await asyncio.sleep(180)  # задержка 3 минуты

    while True:
        try:
            anomalies = await run_full_detection(pool, bot)
            if anomalies:
                critical = [a for a in anomalies if a.severity == "critical"]
                log.info(
                    "anomaly_detector: %d anomalies detected (%d critical)",
                    len(anomalies),
                    len(critical),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("anomaly_detector loop error: %s", e, exc_info=True)

        await asyncio.sleep(_ANOMALY_INTERVAL)
