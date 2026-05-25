"""Background loop: verify crypto payments on-chain and activate subscriptions."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from aiogram import Bot
from config import TON_WALLET, TON_API_KEY, TRON_WALLET, TRON_API_KEY

log = logging.getLogger(__name__)
NANOTON = 1_000_000_000


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot) -> None:
    while True:
        try:
            await _check_pending(pool, http, bot)
        except Exception as e:
            log.exception("payment_checker error: %s", e)
        await asyncio.sleep(30)


async def _check_pending(pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot) -> None:
    await pool.execute(
        "UPDATE payments SET status='expired' "
        "WHERE status='pending' AND expires_at < now()"
    )
    rows = await pool.fetch(
        "SELECT * FROM payments WHERE status IN ('pending','confirming') ORDER BY created_at"
    )
    if not rows:
        return

    ton_rows = [r for r in rows if r["currency"] == "TON" and TON_WALLET]
    trc_rows = [r for r in rows if r["currency"] == "USDT_TRC20" and TRON_WALLET]

    if ton_rows:
        await _check_ton(pool, http, bot, ton_rows)
    if trc_rows:
        await _check_trc20(pool, http, bot, trc_rows)


async def _check_ton(pool, http, bot, payments) -> None:
    try:
        params = {"address": TON_WALLET, "limit": 30}
        headers = {"X-API-Key": TON_API_KEY} if TON_API_KEY else {}
        async with http.get(
            "https://toncenter.com/api/v2/getTransactions",
            params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
    except Exception:
        return

    ref_map = {p["reference"]: p for p in payments}
    for tx in data.get("result", []):
        try:
            in_msg = tx.get("in_msg", {})
            comment = in_msg.get("message", "").strip()
            if comment not in ref_map:
                continue
            payment = ref_map[comment]
            value_nano = int(in_msg.get("value", 0))
            expected_nano = int(float(payment["amount_crypto"]) * NANOTON)
            if value_nano < expected_nano * 0.98:
                continue
            tx_hash = tx.get("transaction_id", {}).get("hash", "")
            await _confirm(pool, bot, payment, tx_hash)
        except Exception:
            continue


async def _check_trc20(pool, http, bot, payments) -> None:
    try:
        headers = {"TRON-PRO-API-KEY": TRON_API_KEY} if TRON_API_KEY else {}
        params = {"limit": 30, "only_to": "true"}
        async with http.get(
            f"https://api.trongrid.io/v1/accounts/{TRON_WALLET}/transactions/trc20",
            params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
    except Exception:
        return

    for payment in payments:
        expected = float(payment["amount_crypto"])
        for tx in data.get("data", []):
            try:
                value = int(tx.get("value", 0)) / 1_000_000
                if abs(value - expected) < 0.02:
                    await _confirm(pool, bot, payment, tx.get("transaction_id", ""))
                    break
            except Exception:
                continue


async def _confirm(pool, bot: Bot, payment, tx_hash: str) -> None:
    updated = await pool.fetchval(
        """
        UPDATE payments SET status='confirmed', tx_hash=$1, confirmed_at=now()
        WHERE id=$2 AND status IN ('pending','confirming')
        RETURNING id
        """,
        tx_hash, payment["id"],
    )
    if not updated:
        return

    await _activate_subscription(pool, payment["user_id"], payment["plan"], payment["period_months"])
    log.info("Payment confirmed: user=%s plan=%s ref=%s", payment["user_id"], payment["plan"], payment["reference"])

    try:
        await bot.send_message(
            payment["user_id"],
            f"🎉 <b>Оплата подтверждена!</b>\n\n"
            f"✅ Подписка <b>{payment['plan'].upper()}</b> на "
            f"{payment['period_months']} мес. активирована!\n\n"
            f"Управление: /subscription",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _activate_subscription(pool, user_id: int, plan: str, months: int) -> None:
    await pool.execute(
        """
        INSERT INTO subscriptions (user_id, plan, expires_at)
        VALUES ($1, $2, now() + ($3 || ' months')::INTERVAL)
        ON CONFLICT (user_id) DO UPDATE SET
            plan       = EXCLUDED.plan,
            expires_at = CASE
                WHEN subscriptions.expires_at > now()
                    THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                ELSE now() + ($3 || ' months')::INTERVAL
            END,
            is_active  = true,
            started_at = CASE
                WHEN subscriptions.expires_at > now() THEN subscriptions.started_at
                ELSE now()
            END
        """,
        user_id, plan, str(months),
    )
