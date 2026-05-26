"""Background service: monitor active account count per owner, alert on low count."""
from __future__ import annotations

import asyncio
import logging

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

_INTERVAL = 3600       # check every hour
_MIN_ACCOUNTS = 2      # alert threshold


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

    for row in rows:
        owner_id = row["owner_id"]
        active_count = row["active_count"]
        try:
            await bot.send_message(
                owner_id,
                f"⚠️ <b>Внимание: мало активных аккаунтов</b>\n\n"
                f"У вас осталось активных TG-аккаунтов: <b>{active_count}</b>\n"
                f"Рекомендуется иметь минимум {_MIN_ACCOUNTS} активных аккаунта "
                f"для корректной работы сервиса.\n\n"
                f"Добавьте аккаунты в разделе <b>Аккаунты</b>.",
            )
            log.info("account_monitor: alerted owner=%s (active=%s)", owner_id, active_count)
        except Exception as exc:
            log.warning("account_monitor: failed to alert owner=%s: %s", owner_id, exc)


async def check_owner_now(pool: asyncpg.Pool, bot: Bot, owner_id: int) -> None:
    """Immediate check for a specific owner — call after deactivating an account."""
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
        owner_id,
    )
    cnt = row["cnt"] if row else 0
    if cnt < _MIN_ACCOUNTS:
        try:
            await bot.send_message(
                owner_id,
                f"⚠️ <b>Аккаунт деактивирован</b>\n\n"
                f"Один из ваших TG-аккаунтов был автоматически деактивирован "
                f"(получен PeerFlood или бан).\n"
                f"Активных аккаунтов осталось: <b>{cnt}</b>.\n\n"
                f"Добавьте новые аккаунты в разделе <b>Аккаунты</b>.",
            )
        except Exception as exc:
            log.warning("account_monitor.check_owner_now: failed for owner=%s: %s", owner_id, exc)


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background loop: check account health every hour."""
    await asyncio.sleep(300)  # startup delay: 5 min
    while True:
        try:
            await _check_and_alert(pool, bot)
        except Exception as exc:
            log.exception("account_monitor error: %s", exc)
        await asyncio.sleep(_INTERVAL)
