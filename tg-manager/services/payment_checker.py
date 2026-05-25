"""Background loop: verify crypto payments on-chain and activate subscriptions."""
from __future__ import annotations
import asyncio
import logging
import os
import aiohttp
import asyncpg
from aiogram import Bot


def _TON_WALLET() -> str: return os.getenv("TON_WALLET", "")
def _TRON_WALLET() -> str: return os.getenv("TRON_WALLET", "")

_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

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

    ton_rows = [r for r in rows if r["currency"] == "TON"]
    trc_rows = [r for r in rows if r["currency"] == "USDT_TRC20"]

    if ton_rows:
        await _check_ton(pool, http, bot, ton_rows)
    if trc_rows:
        await _check_trc20(pool, http, bot, trc_rows)


async def _check_ton(pool, http, bot, payments) -> None:
    wallet = _TON_WALLET()
    if not wallet:
        return
    try:
        async with http.get(
            f"https://tonapi.io/v2/blockchain/accounts/{wallet}/transactions",
            params={"limit": 30},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
    except Exception:
        return

    ref_map = {p["reference"]: p for p in payments}
    for tx in data.get("transactions", []):
        try:
            in_msg = tx.get("in_msg", {})
            comment = (in_msg.get("decoded_body") or {}).get("text", "").strip()
            if comment not in ref_map:
                continue
            payment = ref_map[comment]
            value_nano = int(in_msg.get("value", 0))
            expected_nano = int(float(payment["amount_crypto"]) * NANOTON)
            if value_nano < expected_nano * 0.98:
                continue
            tx_hash = tx.get("hash", "")
            await _confirm(pool, bot, payment, tx_hash)
        except Exception:
            continue


async def _check_trc20(pool, http, bot, payments) -> None:
    wallet = _TRON_WALLET()
    if not wallet:
        return
    try:
        async with http.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "contractAddress": _USDT_CONTRACT,
                "toAddress": wallet,
                "limit": 20,
                "sort": "-timestamp",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
    except Exception:
        return

    used_txids: set[str] = set()
    for payment in payments:
        try:
            expected = float(payment["amount_crypto"])
        except (TypeError, ValueError):
            continue
        for tx in data.get("token_transfers", []):
            try:
                txid = tx.get("transaction_id", "")
                if txid in used_txids:
                    continue
                value = int(tx.get("quant", 0)) / 1_000_000
                if abs(value - expected) < 0.02:
                    await _confirm(pool, bot, payment, txid)
                    used_txids.add(txid)
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
