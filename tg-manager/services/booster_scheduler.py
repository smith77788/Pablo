"""Bot Booster background scheduler: check SMM orders, update positions, notify."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
import asyncpg

from database import db

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 900  # 15 minutes


async def _tick(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    from services.booster_service import smm_check_orders

    try:
        orders = await db.bb_get_active_orders(pool)
    except Exception as exc:
        log.warning("booster_scheduler: failed to fetch active orders: %s", exc)
        return

    for order in orders:
        order_id = order["id"]
        owner_id = order["owner_id"]
        status = order["status"]

        if status == "boosting":
            smm_ids = []
            try:
                smm_ids = json.loads(order["smm_order_ids"] or "[]")
            except (TypeError, ValueError):
                pass

            if not smm_ids:
                continue

            api_url = order.get("api_url") or ""
            api_key = order.get("api_key") or ""
            if not api_url or not api_key:
                continue

            statuses = await smm_check_orders(http, api_url, api_key, smm_ids)
            if not statuses:
                continue

            all_done = all(
                str(v.get("status", "")).lower() in ("completed", "complete", "done")
                for v in statuses.values()
            )
            all_failed = all(
                str(v.get("status", "")).lower() in ("canceled", "cancelled", "failed", "error")
                for v in statuses.values()
            )

            if all_done:
                await db.bb_update_order_status(pool, order_id, "checking")
                await db.bb_log(pool, owner_id, "orders", f"Заказ #{order_id}: SMM выполнен, переходим к проверке позиции", "INFO")
            elif all_failed:
                await db.bb_update_order_status(pool, order_id, "failed", notes="SMM заказ отменён/провален")
                await db.bb_log(pool, owner_id, "orders", f"Заказ #{order_id}: SMM заказ провалился", "WARN")

        elif status == "checking":
            await db.bb_log(pool, owner_id, "orders", f"Заказ #{order_id}: проверяем позицию в топе (ручная проверка через Чекер)", "INFO")


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            await _tick(pool, http)
        except Exception as exc:
            log.exception("booster_scheduler error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL)
