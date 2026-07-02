"""Background service: monitor active account count per owner, alert on low count.

Also: ping Telegram to verify session liveness, mark dead sessions as
session_expired, and notify account owners.
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncpg
from aiogram import Bot
from database import db
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_INTERVAL = 3600  # check every hour
_MIN_ACCOUNTS = 2  # alert threshold
_LOW_TRUST_THRESHOLD = 0.3  # trust_score below this triggers alert
_STALE_RUNNING_HOURS = 3  # running ops older than this are considered stuck
_ALERT_COOLDOWN = 86400  # 24h between repeated low-account alerts per owner

# In-memory cooldown: owner_id → last_alert_ts
_low_account_alerted: dict[int, float] = {}

# In-memory cooldown for session_expired alerts: account_id → last_alert_ts
_session_expired_alerted: dict[int, float] = {}


async def _check_and_alert(pool: asyncpg.Pool, bot: Bot) -> None:
    """Find owners with fewer than _MIN_ACCOUNTS active accounts and notify them."""
    rows = await pool.fetch(
        """
        SELECT ta.owner_id, COUNT(*) AS active_count
        FROM tg_accounts ta
        WHERE ta.is_active = true
        GROUP BY ta.owner_id
        HAVING COUNT(*) < $1
        """,
        _MIN_ACCOUNTS,
    )
    if not rows:
        return

    now = time.time()
    for row in rows:
        owner_id = row["owner_id"]
        active_count = row["active_count"]
        last_sent = _low_account_alerted.get(owner_id, 0)
        if now - last_sent < _ALERT_COOLDOWN:
            continue
        _low_account_alerted[owner_id] = now
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "flood_warning",
            f"⚠️ <b>Внимание: мало активных аккаунтов</b>\n\n"
            f"У вас осталось активных TG-аккаунтов: <b>{active_count}</b>\n"
            f"Рекомендуется иметь минимум {_MIN_ACCOUNTS} активных аккаунта "
            f"для корректной работы сервиса.\n\n"
            f"Добавьте аккаунты в разделе <b>Аккаунты</b>.",
        )
        log.info(
            "account_monitor: alerted owner=%s (active=%s)", owner_id, active_count
        )


async def _check_low_trust(pool: asyncpg.Pool, bot: Bot) -> None:
    """Alert owners when accounts drop to critically low trust_score."""
    try:
        rows = await pool.fetch(
            """
            SELECT ta.owner_id, ta.phone, ta.first_name, ta.username,
                   ta.trust_score, ta.flood_count_7d
            FROM tg_accounts ta
            WHERE ta.is_active = true
              AND ta.trust_score < $1
              AND (ta.last_low_trust_alert IS NULL
                   OR ta.last_low_trust_alert < NOW() - INTERVAL '6 hours')
            """,
            _LOW_TRUST_THRESHOLD,
        )
    except Exception as e:
        log.debug("low_trust_check: %s (column may not exist yet)", e)
        return

    by_owner: dict[int, list] = {}
    for r in rows:
        by_owner.setdefault(r["owner_id"], []).append(r)

    for owner_id, accs in by_owner.items():
        names = [r["username"] or r["first_name"] or r["phone"] or "id?" for r in accs]
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "restriction",
            f"🔴 <b>Критически низкий trust_score</b>\n\n"
            f"Аккаунты: <b>{', '.join(names[:5])}</b>\n"
            f"Trust score ниже {_LOW_TRUST_THRESHOLD:.1f} — высокий риск бана.\n\n"
            f"Рекомендации:\n"
            f"• Не запускайте операции через эти аккаунты 48ч\n"
            f"• Откройте Health Dashboard → 💡 Рекомендации\n"
            f"• Проверьте аккаунты вручную в Telegram",
        )
        # Mark as alerted (best-effort)
        try:
            await pool.execute(
                "UPDATE tg_accounts SET last_low_trust_alert=NOW() "
                "WHERE owner_id=$1 AND trust_score < $2 AND is_active=true",
                owner_id,
                _LOW_TRUST_THRESHOLD,
            )
        except Exception:
            log_exc_swallow(
                log, "сбой алерта low trust — не удалось обновить last_low_trust_alert"
            )
        log.info(
            "account_monitor: low trust alert for owner=%s (%d accounts)",
            owner_id,
            len(accs),
        )


async def _recover_stuck_operations(pool: asyncpg.Pool, bot: Bot) -> None:
    """Mark operations stuck in 'running' for too long as failed."""
    try:
        stuck = await pool.fetch(
            """
            SELECT id, owner_id, op_type
            FROM operation_queue
            WHERE status = 'running'
              AND started_at < NOW() - ($1 * INTERVAL '1 hour')
            """,
            _STALE_RUNNING_HOURS,
        )
        for row in stuck:
            await pool.execute(
                "UPDATE operation_queue SET status='failed', finished_at=NOW(), "
                "error_msg='Операция зависла (таймаут 3ч) — перезапустите вручную' "
                "WHERE id=$1 AND status='running'",
                row["id"],
            )
            await db.notify_if_enabled(
                pool,
                bot,
                row["owner_id"],
                "op_complete",
                f"⚠️ <b>Операция #{row['id']} зависла</b>\n\n"
                f"Тип: {row['op_type']}\n"
                f"Операция выполнялась более {_STALE_RUNNING_HOURS}ч без завершения.\n"
                f"Статус изменён на failed. Перезапустите из раздела Operations → Отчёты.",
            )
            log.warning("account_monitor: marked stuck op id=%d as failed", row["id"])
    except Exception as exc:
        log.debug("account_monitor: stuck ops check error: %s", exc)


async def _check_dead_sessions(pool: asyncpg.Pool, bot: Bot) -> None:
    """Ping Telegram for accounts whose session hasn't been verified recently.

    Checks up to 5 accounts per cycle (those not checked in the last 3 hours).
    On auth failure: sets acc_status='session_expired', is_active=FALSE,
    and notifies the owner.  Uses should_persist_account_status() to avoid
    flip-flopping on transient errors.
    """
    from services.account_manager import check_account_status_full, should_persist_account_status

    try:
        accounts = await pool.fetch(
            """SELECT id, owner_id, session_str, phone, first_name, username,
                      device_model, system_version, app_version, proxy_id, acc_status
               FROM tg_accounts
               WHERE is_active = TRUE
                 AND session_str IS NOT NULL AND session_str != ''
                 AND (last_real_check_at IS NULL
                      OR last_real_check_at < NOW() - INTERVAL '3 hours')
               ORDER BY COALESCE(last_real_check_at, '2000-01-01') ASC
               LIMIT 5""",
        )
    except Exception as exc:
        log.debug("_check_dead_sessions: query error: %s", exc)
        return

    if not accounts:
        return

    log.info("account_monitor: dead-session check — %d accounts", len(accounts))
    now = time.time()

    for acc in accounts:
        try:
            result = await asyncio.wait_for(
                check_account_status_full(
                    acc["session_str"], dict(acc), check_spambot=False
                ),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            log.debug("account_monitor: session ping timeout acc=%d", acc["id"])
            continue
        except Exception as exc:
            log_exc_swallow(log, "account_monitor dead-session check acc=%d: %s", acc["id"], exc)
            continue

        status = result.get("status", "active")
        auth_error = result.get("auth_error", False)

        # Always update the check timestamp
        try:
            await pool.execute(
                "UPDATE tg_accounts SET last_real_check_at=now() WHERE id=$1",
                acc["id"],
            )
        except Exception:
            log_exc_swallow(log, "account_monitor: failed to update last_real_check_at acc=%d", acc["id"])

        if result.get("no_session"):
            continue

        if not should_persist_account_status(
            status, auth_error=auth_error, has_session=True
        ):
            continue

        # Session is confirmed dead
        if status in ("session_expired", "banned", "deactivated") and auth_error:
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status=$1, is_active=FALSE WHERE id=$2",
                    status,
                    acc["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, "account_monitor: failed to update dead acc=%d", acc["id"]
                )

            # Notify owner — deduplicated per 24h
            last_sent = _session_expired_alerted.get(acc["id"], 0)
            if now - last_sent >= _ALERT_COOLDOWN:
                _session_expired_alerted[acc["id"]] = now
                label = acc.get("username") or acc.get("first_name") or acc.get("phone") or str(acc["id"])
                await db.notify_if_enabled(
                    pool,
                    bot,
                    acc["owner_id"],
                    "restriction",
                    f"🔴 <b>Сессия аккаунта истекла</b>\n\n"
                    f"Аккаунт: <b>@{label}</b>\n"
                    f"Статус: <b>{status}</b>\n\n"
                    "Telegram отклонил авторизацию — сессия мертва.\n"
                    "Аккаунт деактивирован автоматически.\n\n"
                    "Переимпортируйте сессию в разделе <b>Аккаунты</b>.",
                )
                log.warning(
                    "account_monitor: dead session acc=%d owner=%d status=%s",
                    acc["id"],
                    acc["owner_id"],
                    status,
                )

        await asyncio.sleep(2)  # small pause between account checks


async def check_owner_now(pool: asyncpg.Pool, bot: Bot, owner_id: int) -> None:
    """Immediate check for a specific owner — call after deactivating an account."""
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
        owner_id,
    )
    cnt = row["cnt"] if row else 0
    if cnt < _MIN_ACCOUNTS:
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "restriction",
            f"⚠️ <b>Аккаунт деактивирован</b>\n\n"
            f"Один из ваших TG-аккаунтов был автоматически деактивирован "
            f"(получен PeerFlood или бан).\n"
            f"Активных аккаунтов осталось: <b>{cnt}</b>.\n\n"
            f"Добавьте новые аккаунты в разделе <b>Аккаунты</b>.",
        )


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background loop: check account health every hour."""
    await asyncio.sleep(300)  # startup delay: 5 min
    cycle = 0
    while True:
        try:
            await _check_and_alert(pool, bot)
            await _check_low_trust(pool, bot)
            # Check for stuck operations every 3 cycles (every 3 hours)
            if cycle % 3 == 0:
                await _recover_stuck_operations(pool, bot)
            # Ping Telegram sessions every cycle to detect dead sessions
            await _check_dead_sessions(pool, bot)
            cycle += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("account_monitor error: %s", exc)
        await asyncio.sleep(_INTERVAL)
