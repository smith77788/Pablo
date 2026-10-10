"""Background scheduler for Bot Promotion Platform.

Runs every 15 minutes:
- Refreshes aging bot statuses (aging → ready when 21 days elapsed)
- Checks SMM panel order statuses for boosting orders
- Sends Telegram notifications on key events (topped, error, complete)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from database import db
from services.logger import log_exc_swallow
from services import smm_panel as smm_svc

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 900  # 15 minutes


async def run(pool: asyncpg.Pool, bot) -> None:
    """Main scheduler loop. Designed to run as a long-lived background task."""
    log.info("promo_scheduler: started (interval=%ds)", _CHECK_INTERVAL)
    await asyncio.sleep(30)  # delay startup to let DB settle
    while True:
        try:
            await _tick(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc_swallow(log, "promo_scheduler tick error")
        await asyncio.sleep(_CHECK_INTERVAL)


async def _tick(pool: asyncpg.Pool, bot) -> None:
    await _refresh_aging_bots(pool, bot)
    await _check_smm_orders(pool, bot)


# ── Aging bots ────────────────────────────────────────────────────────────────

async def _refresh_aging_bots(pool: asyncpg.Pool, bot) -> None:
    try:
        rows = await pool.fetch(
            """UPDATE bot_warehouse
               SET status='ready', updated_at=NOW()
               WHERE status='aging' AND ready_at <= NOW()
               RETURNING id, owner_id, bot_username, ready_at"""
        )
        for r in rows:
            await db.promo_log(
                pool, r["owner_id"], "scheduler",
                f"Бот @{r['bot_username']} созрел → готов к работе",
            )
            await _notify(bot, r["owner_id"],
                          f"✅ <b>Бот @{r['bot_username']} готов!</b>\n\n"
                          f"21 день созревания прошёл. Бот доступен для продвижения.\n"
                          f"Откройте /promo → Склад ботов.")
    except Exception:
        log_exc_swallow(log, "promo_scheduler: aging refresh error")


# ── SMM order status checks ───────────────────────────────────────────────────

async def _check_smm_orders(pool: asyncpg.Pool, bot) -> None:
    try:
        orders = await pool.fetch(
            """SELECT o.*, p.api_url, p.api_key_enc, p.name AS panel_name
               FROM promo_orders o
               JOIN smm_panels p ON p.id = o.smm_panel_id
               WHERE o.status = 'boosting'
                 AND o.smm_order_id IS NOT NULL
                 AND o.smm_panel_id IS NOT NULL"""
        )
        if not orders:
            return

        # Group by panel to batch requests
        by_panel: dict[int, list] = {}
        for o in orders:
            pid = o["smm_panel_id"]
            by_panel.setdefault(pid, []).append(o)

        for panel_id, panel_orders in by_panel.items():
            first = panel_orders[0]
            client = smm_svc.make_client(first["api_url"], first["api_key_enc"])

            order_ids = [o["smm_order_id"] for o in panel_orders]
            try:
                if len(order_ids) == 1:
                    result = await client.get_order_status(order_ids[0])
                    statuses = {order_ids[0]: result}
                else:
                    result = await client.get_multiple_statuses(order_ids)
                    if isinstance(result, dict) and not result.get("error"):
                        statuses = result
                    else:
                        statuses = {}
            except Exception:
                log_exc_swallow(log, "promo_scheduler: panel %d status check failed", panel_id)
                continue

            for o in panel_orders:
                smm_id = o["smm_order_id"]
                raw = statuses.get(smm_id, {})
                if not raw or raw.get("error"):
                    continue
                await _process_order_status(pool, bot, dict(o), raw, first["panel_name"])

    except Exception:
        log_exc_swallow(log, "promo_scheduler: smm check error")


async def _process_order_status(
    pool: asyncpg.Pool, bot, order: dict, raw: dict, panel_name: str
) -> None:
    raw_status = raw.get("status", "")
    remains = raw.get("remains", raw.get("remain", 0))
    start_count = raw.get("start_count", 0)
    charge = raw.get("charge", 0)

    try:
        remains_int = int(remains or 0)
        start_int = int(start_count or 0)
    except (TypeError, ValueError):
        remains_int = 0
        start_int = 0

    current = start_int + (int(order.get("target_subs") or 0) - remains_int)
    current = max(current, 0)

    normalized = smm_svc.normalize_status(raw_status)

    await db.promo_log(
        pool, order["owner_id"], "booster",
        f"Заказ #{order['id']} ({panel_name}): {normalized}, осталось {remains_int}",
        order_id=order["id"],
        meta={"raw_status": raw_status, "remains": remains_int},
    )

    if raw_status in ("Completed", "Partial"):
        new_status = "checking"
        await db.promo_update_order_status(
            pool, order["id"], new_status, current_subs=current
        )
        await _notify(bot, order["owner_id"],
                      f"{'🏁' if raw_status == 'Completed' else '⚠️'} "
                      f"<b>Накрутка {'завершена' if raw_status == 'Completed' else 'частичная'}</b>\n\n"
                      f"Заказ #{order['id']}: <code>{order['keyword']}</code>\n"
                      f"Подписчики: ~{current} · Статус панели: {normalized}\n\n"
                      f"→ Теперь нужно проверить позицию в поиске. /promo")
    elif raw_status == "Cancelled":
        await db.promo_update_order_status(pool, order["id"], "waiting", current_subs=current)
        await _notify(bot, order["owner_id"],
                      f"❌ <b>Заказ на панели отменён</b>\n\n"
                      f"Заказ #{order['id']}: <code>{order['keyword']}</code>\n"
                      f"Панель: {panel_name} · SMM-заказ #{order['smm_order_id']}\n\n"
                      f"Заказ возвращён в статус «Ожидает». /promo")
    else:
        await db.promo_update_order_status(pool, order["id"], "boosting", current_subs=current)


# ── Notifications ─────────────────────────────────────────────────────────────

async def _notify(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception:
        log_exc_swallow(log, "promo_scheduler: notify failed for user %d", user_id)
