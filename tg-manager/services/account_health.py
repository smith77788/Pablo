"""
Account Health Engine — комплексная оценка здоровья аккаунтов.

Tracks:
- health_score (0-100): общая оценка надёжности аккаунта
- load_score (0-100): текущая нагрузка (чем выше, тем меньше использовать)
- warmup_state: raw/warming/ready/veteran
- restriction_history: история блокировок
- operation success/fail rates

Интегрируется с tg_accounts (trust_score, cooldown_until, acc_status),
account_flood_log, account_trust_history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import asyncpg

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


class WarmupState(str, Enum):
    RAW = "raw"  # новый аккаунт, никогда не использовался
    WARMING = "warming"  # в процессе разогрева
    READY = "ready"  # готов к операциям
    VETERAN = "veteran"  # проверенный, много успешных операций


@dataclass
class AccountHealth:
    account_id: int
    health_score: float = 100.0  # 0-100, выше = лучше
    load_score: float = 0.0  # 0-100, выше = более нагружен
    warmup_state: WarmupState = WarmupState.RAW
    total_ops: int = 0
    success_ops: int = 0
    fail_ops: int = 0
    flood_events_7d: int = 0
    spamblock_events: int = 0
    restriction_count: int = 0
    last_updated: float = field(default_factory=time.monotonic)
    suitability: dict[str, bool] = field(
        default_factory=lambda: {
            "invite": True,
            "dm": True,
            "create": True,
            "post": True,
            "join": True,
        }
    )


# In-memory health cache
_health_cache: dict[int, AccountHealth] = {}


def get_health(account_id: int) -> AccountHealth:
    if account_id not in _health_cache:
        _health_cache[account_id] = AccountHealth(account_id=account_id)
    return _health_cache[account_id]


def compute_health_score(
    trust_score: float,
    flood_count_7d: int,
    spamblock: bool,
    success_rate: float,  # 0.0-1.0
    days_active: int,
) -> float:
    """Вычисляет health_score (0-100) по нескольким факторам."""
    score = 100.0

    # Trust score (0-1 → 0-30 points)
    score -= (1.0 - min(1.0, trust_score)) * 30

    # Flood penalties
    score -= min(flood_count_7d * 5, 30)

    # SpamBot restriction
    if spamblock:
        score -= 40

    # Success rate factor (0-20 points)
    score -= (1.0 - success_rate) * 20

    # Age bonus (up to +10 for accounts active 30+ days)
    score += min(days_active / 3.0, 10)

    return max(0.0, min(100.0, score))


def estimate_warmup_state(
    days_active: int, total_ops: int, trust_score: float
) -> WarmupState:
    if trust_score < 0.2 or total_ops == 0:
        return WarmupState.RAW
    if days_active < 3 or total_ops < 10:
        return WarmupState.WARMING
    if trust_score >= 0.8 and total_ops >= 100:
        return WarmupState.VETERAN
    return WarmupState.READY


def update_after_success(account_id: int, action_type: str = "default") -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.success_ops += 1
    health.load_score = min(100.0, health.load_score + 1.0)
    # Small health boost on success
    health.health_score = min(100.0, health.health_score + 0.2)
    health.last_updated = time.monotonic()


def update_after_failure(
    account_id: int,
    action_type: str = "default",
    is_flood: bool = False,
    is_spamblock: bool = False,
    is_ban: bool = False,
) -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.fail_ops += 1

    if is_flood:
        health.flood_events_7d += 1
        health.health_score -= 5
        health.load_score += 10

    if is_spamblock:
        health.spamblock_events += 1
        health.health_score -= 25
        health.suitability["dm"] = False
        health.suitability["invite"] = False

    if is_ban:
        health.health_score = 0
        health.suitability = {k: False for k in health.suitability}

    health.health_score = max(0.0, health.health_score)
    health.last_updated = time.monotonic()


async def load_from_db(pool: asyncpg.Pool, owner_id: int) -> int:
    """Загружает статистику здоровья всех аккаунтов из БД."""
    rows = await pool.fetch(
        """SELECT
               a.id,
               a.trust_score,
               COALESCE(a.flood_count_7d, 0) AS flood_count_7d,
               COALESCE(a.acc_status, 'active') AS acc_status,
               EXTRACT(DAY FROM NOW() - a.added_at)::int AS days_active,
               COUNT(DISTINCT fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '7d') AS floods_7d,
               COUNT(DISTINCT oa.id) FILTER (WHERE oa.result='success') AS ops_ok,
               COUNT(DISTINCT oa.id) FILTER (WHERE oa.result!='success' AND oa.result IS NOT NULL) AS ops_fail
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id = a.id
           LEFT JOIN operation_audit oa ON oa.account_id = a.id
           WHERE a.owner_id = $1
             AND a.is_active = TRUE
           GROUP BY a.id, a.trust_score, a.flood_count_7d, a.acc_status, a.added_at""",
        owner_id,
    )

    loaded = 0
    for row in rows:
        acc_id = row["id"]
        total = (row["ops_ok"] or 0) + (row["ops_fail"] or 0)
        success_rate = (row["ops_ok"] or 0) / max(total, 1)
        is_spamblock = row["acc_status"] in ("spamblock", "cooldown")

        score = compute_health_score(
            trust_score=float(row["trust_score"] or 1.0),
            flood_count_7d=row["floods_7d"] or 0,
            spamblock=is_spamblock,
            success_rate=success_rate,
            days_active=row["days_active"] or 0,
        )

        warmup = estimate_warmup_state(
            row["days_active"] or 0,
            total,
            float(row["trust_score"] or 1.0),
        )

        health = get_health(acc_id)
        health.health_score = score
        health.warmup_state = warmup
        health.total_ops = total
        health.success_ops = row["ops_ok"] or 0
        health.fail_ops = row["ops_fail"] or 0
        health.flood_events_7d = row["floods_7d"] or 0
        health.load_score = min(100.0, (row["floods_7d"] or 0) * 5.0)

        if is_spamblock:
            health.suitability["dm"] = False
            health.suitability["invite"] = False

        loaded += 1

    log.info("account_health: loaded %d accounts for owner=%d", loaded, owner_id)
    return loaded


async def get_sorted_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    limit: int = 20,
) -> list[dict]:
    """Возвращает аккаунты отсортированные по пригодности для action_type."""
    rows = await pool.fetch(
        """SELECT a.id, a.session_str, a.phone, a.first_name, a.trust_score,
                  a.device_model, a.system_version, a.app_version,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.owner_id = $1
             AND a.is_active = TRUE
             AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
             AND COALESCE(a.acc_status, 'active') NOT IN ('banned', 'deactivated', 'session_expired')
           ORDER BY a.trust_score DESC NULLS LAST
           LIMIT $2""",
        owner_id,
        limit * 2,
    )

    scored = []
    for row in rows:
        health = get_health(row["id"])
        # Проверяем пригодность для данного типа действия
        if action_type in health.suitability and not health.suitability[action_type]:
            continue
        scored.append((health.health_score - health.load_score / 2, dict(row)))

    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored[:limit]]


def get_health_summary(account_ids: list[int]) -> list[dict]:
    """Сводная таблица здоровья аккаунтов для UI."""
    result = []
    for acc_id in account_ids:
        h = get_health(acc_id)
        result.append(
            {
                "account_id": acc_id,
                "health_score": round(h.health_score, 1),
                "load_score": round(h.load_score, 1),
                "warmup_state": h.warmup_state.value,
                "flood_events_7d": h.flood_events_7d,
                "success_rate": round(h.success_ops / max(h.total_ops, 1) * 100, 1),
                "suitability": h.suitability,
            }
        )
    return sorted(result, key=lambda x: -x["health_score"])


async def _persist_health_snapshots(pool: asyncpg.Pool) -> int:
    """Сохраняет текущие health_score из in-memory кеша в account_health_history."""
    if not _health_cache:
        return 0

    import json as _json

    batch = []
    for acc_id, health in _health_cache.items():
        batch.append(
            (
                acc_id,
                0,  # owner_id будет заполнен из БД через ON CONFLICT или отдельным запросом
                round(health.health_score, 2),
                round(health.load_score, 2),
                0.0,  # trust_score — заполнится ниже
                health.flood_events_7d,
                health.success_ops,
                health.fail_ops,
                health.warmup_state.value,
                _json.dumps(health.suitability, ensure_ascii=False),
            )
        )

    if not batch:
        return 0

    # Получаем owner_id и trust_score для аккаунтов одним запросом
    acc_ids = [b[0] for b in batch]
    acc_info = await pool.fetch(
        "SELECT id, owner_id, trust_score FROM tg_accounts WHERE id = ANY($1::int[])",
        acc_ids,
    )
    info_map: dict[int, tuple[int, float]] = {}
    for row in acc_info:
        info_map[row["id"]] = (row["owner_id"], float(row["trust_score"] or 0))

    written = 0
    for item in batch:
        acc_id = item[0]
        if acc_id not in info_map:
            continue
        owner_id, trust_score = info_map[acc_id]
        try:
            await pool.execute(
                """INSERT INTO account_health_history
                       (account_id, owner_id, health_score, load_score, trust_score,
                        flood_events_7d, success_ops, fail_ops, warmup_state, suitability)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)""",
                acc_id,
                owner_id,
                item[2],
                item[3],
                trust_score,
                item[5],
                item[6],
                item[7],
                item[8],
                item[9],
            )
            written += 1
        except Exception:
            log_exc_swallow(
                log,
                "Сбой записи health-снапшота — таблица может ещё не существовать",
                account_id=acc_id,
            )

    return written


async def _cleanup_old_health_history(pool: asyncpg.Pool) -> int:
    """Удаляет health-снапшоты старше 30 дней."""
    try:
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM account_health_history "
            "WHERE recorded_at < NOW() - INTERVAL '30 days' RETURNING 1) "
            "SELECT COUNT(*) FROM d"
        )
        return int(deleted or 0)
    except Exception:
        log_exc_swallow(log, "Сбой очистки старых health-снапшотов")
        return 0


async def _run_spambot_check_cycle(pool: asyncpg.Pool) -> None:
    """Периодическая проверка acc_status через SpamBot (раз в 6 часов на аккаунт).

    Обновляет acc_status, trust_score в DB. При обнаружении spamblock — снижает trust.
    Не уведомляет — уведомления идут из account_monitor.
    """
    from services.account_manager import check_account_status_full

    # Аккаунты с session_str, не проверявшиеся более 6 часов
    accounts = await pool.fetch(
        """SELECT id, session_str, phone, first_name, username,
                  device_model, system_version, app_version, proxy_id
           FROM tg_accounts
           WHERE is_active=TRUE
             AND session_str IS NOT NULL AND session_str != ''
             AND (last_real_check_at IS NULL
                  OR last_real_check_at < NOW() - INTERVAL '6 hours')
           ORDER BY COALESCE(last_real_check_at, '2000-01-01') ASC
           LIMIT 10""",
    )
    if not accounts:
        return

    log.info(
        "account_health: spambot check cycle — %d accounts to check", len(accounts)
    )

    for acc in accounts:
        try:
            result = await asyncio.wait_for(
                check_account_status_full(
                    acc["session_str"], dict(acc), check_spambot=True
                ),
                timeout=30.0,
            )
            status = result.get("status", "active")
            auth_error = result.get("auth_error", False)

            # Не обновляем acc_status для no_session — аккаунт просто не импортирован
            if result.get("no_session"):
                continue

            # Только подтверждённые статусы пишем в БД — session_expired без auth_error игнорируем
            if status in ("active", "spamblock", "cooldown"):
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status=$1, last_real_check_at=now() WHERE id=$2",
                    status,
                    acc["id"],
                )
            elif status in ("banned", "deactivated") and auth_error:
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status=$1, last_real_check_at=now() WHERE id=$2",
                    status,
                    acc["id"],
                )
            elif status == "session_expired" and auth_error:
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status=$1, last_real_check_at=now() WHERE id=$2",
                    status,
                    acc["id"],
                )
            else:
                # Любой спорный статус — только обновляем время проверки, не меняем acc_status
                await pool.execute(
                    "UPDATE tg_accounts SET last_real_check_at=now() WHERE id=$1",
                    acc["id"],
                )

            if status == "spamblock":
                await pool.execute(
                    "UPDATE tg_accounts SET trust_score=LEAST(COALESCE(trust_score,1.0), 0.3) WHERE id=$1",
                    acc["id"],
                )
                log.warning("account_health: spamblock detected acc=%d", acc["id"])
            elif status in ("banned", "deactivated", "session_expired") and auth_error:
                # Деактивируем только при явном auth_error от Telegram
                await pool.execute(
                    "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc["id"]
                )
                log.warning(
                    "account_health: acc=%d deactivated status=%s (auth_error)",
                    acc["id"],
                    status,
                )
        except asyncio.TimeoutError:
            log.debug("account_health: spambot check timeout acc=%d", acc["id"])
        except Exception as e:
            log_exc_swallow(
                log, "account_health spambot check acc=%d: %s", acc["id"], e
            )

        await asyncio.sleep(3)  # небольшая пауза между аккаунтами


async def run_health_check_loop(pool: asyncpg.Pool, interval_s: int = 3600) -> None:
    """Фоновый цикл: каждый час пересчитывает здоровье и сохраняет снапшоты.

    Использует time-anchored sleep чтобы избежать наслаивания циклов:
    если сам health check занял N секунд, следующий sleep = max(0, interval_s - N).
    """
    cycle = 0
    while True:
        started_at = time.monotonic()
        try:
            owners = await pool.fetch(
                "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
            )
            total_loaded = 0
            for row in owners:
                total_loaded += await load_from_db(pool, row["owner_id"])

            # Сохраняем снапшоты health_score в БД для трендов
            if total_loaded > 0:
                written = await _persist_health_snapshots(pool)
                if written:
                    log.debug("account_health: persisted %d snapshots", written)

            # Чистка старых снапшотов раз в сутки (24 цикла)
            if cycle % 24 == 0:
                cleaned = await _cleanup_old_health_history(pool)
                if cleaned:
                    log.debug("account_health: cleaned %d old snapshots", cleaned)

            # SpamBot проверка: каждые 6 часов проверяем аккаунты через реальный Telegram
            if cycle % 6 == 0:
                try:
                    await _run_spambot_check_cycle(pool)
                except Exception:
                    log_exc_swallow(log, "account_health: spambot check cycle failed")

            cycle += 1
        except Exception:
            log.exception("account_health loop error")
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(0.0, interval_s - elapsed))
