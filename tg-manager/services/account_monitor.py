"""Background service: monitor active account count per owner, alert on low count."""

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
            cycle += 1
        except Exception as exc:
            log.exception("account_monitor error: %s", exc)
        await asyncio.sleep(_INTERVAL)
