"""Background loop: verify crypto payments on-chain and activate subscriptions."""

from __future__ import annotations
import asyncio
import logging
import os
import aiohttp
import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
NANOTON = 1_000_000_000
_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _TON_WALLET() -> str:
    return os.getenv("TON_WALLET", "")


def _TRON_WALLET() -> str:
    return os.getenv("TRON_WALLET", "")


def _TON_API_KEY() -> str:
    return os.getenv("TON_API_KEY", "")


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot) -> None:
    while True:
        try:
            await _check_pending(pool, http, bot)
        except Exception as e:
            log.exception("payment_checker error: %s", e)
        await asyncio.sleep(30)


async def _check_pending(
    pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot
) -> None:
    # Expire old pending payments
    await pool.execute(
        "UPDATE payments SET status='expired' WHERE status='pending' AND expires_at < now()"
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
    headers = {}
    api_key = _TON_API_KEY()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with http.get(
            f"https://tonapi.io/v2/blockchain/accounts/{wallet}/transactions",
            params={"limit": 50},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 401:
                log.warning("TON API: invalid API key (401). Check TON_API_KEY.")
                return
            if r.status == 429:
                log.warning(
                    "TON API: rate limited (429). Set TON_API_KEY for higher limits."
                )
                return
            if r.status != 200:
                log.warning("TON API returned %s", r.status)
                return
            data = await r.json()
    except asyncio.TimeoutError:
        log.warning("TON API timeout")
        return
    except Exception as e:
        log.warning("TON API error: %s", e)
        return

    ref_map = {p["reference"]: p for p in payments}
    for tx in data.get("transactions", []):
        try:
            in_msg = tx.get("in_msg", {})
            if not in_msg:
                continue
            # Try multiple locations for the comment
            decoded = in_msg.get("decoded_body") or {}
            comment = decoded.get("text", "") or in_msg.get("comment", "") or ""
            comment = comment.strip()
            if not comment or comment not in ref_map:
                continue
            payment = ref_map[comment]
            value_nano = int(in_msg.get("value", 0))
            expected_nano = int(float(payment["amount_crypto"]) * NANOTON)
            # Accept if ≥98% of expected amount
            if value_nano < expected_nano * 0.98:
                log.info(
                    "TON partial payment: ref=%s got=%d expected=%d",
                    comment,
                    value_nano,
                    expected_nano,
                )
                continue
            tx_hash = tx.get("hash", "")
            await _confirm(pool, bot, payment, tx_hash)
        except Exception as e:
            log.debug("TON tx parse error: %s", e)
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
                "limit": 50,
                "sort": "-timestamp",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                log.warning("TRON API returned %s", r.status)
                return
            data = await r.json()
    except asyncio.TimeoutError:
        log.warning("TRON API timeout")
        return
    except Exception as e:
        log.warning("TRON API error: %s", e)
        return

    used_txids: set[str] = set()
    # Pre-collect already-used tx hashes from DB to avoid re-confirming
    try:
        existing = await pool.fetch(
            "SELECT tx_hash FROM payments WHERE status='confirmed' AND tx_hash IS NOT NULL"
        )
        used_txids = {r["tx_hash"] for r in existing if r["tx_hash"]}
    except Exception:
        log_exc_swallow(log, "Сбой получения used_txids из payments")

    for payment in payments:
        try:
            expected = float(payment["amount_crypto"])
        except (TypeError, ValueError):
            continue
        for tx in data.get("token_transfers", []):
            try:
                txid = tx.get("transaction_id", "")
                if not txid or txid in used_txids:
                    continue
                value = int(tx.get("quant", 0)) / 1_000_000
                if abs(value - expected) < 0.02:
                    await _confirm(pool, bot, payment, txid)
                    used_txids.add(txid)
                    break
            except Exception as e:
                log.debug("TRON tx parse error: %s", e)
                continue


async def _confirm(pool, bot: Bot, payment, tx_hash: str) -> None:
    updated = await pool.fetchval(
        """UPDATE payments SET status='confirmed', tx_hash=$1, confirmed_at=now()
           WHERE id=$2 AND status IN ('pending','confirming')
           RETURNING id""",
        tx_hash,
        payment["id"],
    )
    if not updated:
        return  # already confirmed or expired

    user_id = payment["user_id"]
    if payment["plan"] == "strike":
        await pool.execute(
            "CREATE TABLE IF NOT EXISTS strike_access "
            "(user_id BIGINT PRIMARY KEY, purchased_at TIMESTAMPTZ DEFAULT now(), "
            "payment_ref TEXT, granted_by BIGINT)"
        )
        await pool.execute(
            """INSERT INTO strike_access (user_id, payment_ref)
               VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING""",
            user_id,
            payment["reference"],
        )
    if payment["plan"] != "strike":
        await _activate_subscription(
            pool, user_id, payment["plan"], payment["period_months"]
        )
    log.info(
        "Payment confirmed: user=%s plan=%s months=%s ref=%s tx=%s",
        user_id,
        payment["plan"],
        payment["period_months"],
        payment["reference"],
        tx_hash[:16] if tx_hash else "",
    )

    # Referral system: mark paid + check rewards for referrer
    try:
        from database import db as _db

        referrer_id = await _db.mark_referral_paid(pool, user_id)
        if referrer_id:
            await _db.check_and_grant_rewards(pool, referrer_id, bot)
            # Notify referrer about the paid conversion
            try:
                await bot.send_message(
                    referrer_id,
                    "💳 <b>Один из ваших рефералов оплатил подписку!</b>\n\n"
                    "Проверьте прогресс и награды: /referral",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log, "Сбой уведомления реферера о платеже", referrer_id=referrer_id
                )
    except Exception as e:
        log.warning("Referral paid hook error: %s", e)

    try:
        em = {"starter": "⭐", "pro": "🚀", "enterprise": "👑", "strike": "⚔️"}.get(
            payment["plan"], "💳"
        )
        if payment["plan"] == "strike":
            msg = (
                "⚔️ <b>Strike Module активирован!</b>\n\n"
                "Вы получили пожизненный доступ к модулю массовой зачистки нелегального контента.\n\n"
                "Перейти: /menu → ⚔️ Strike"
            )
        else:
            msg = (
                f"🎉 <b>Оплата подтверждена!</b>\n\n"
                f"{em} Подписка <b>{payment['plan'].upper()}</b> на "
                f"{payment['period_months']} мес. активирована!\n\n"
                f"Управление: /subscription"
            )
        await bot.send_message(
            payment["user_id"],
            msg,
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой уведомления пользователя о платеже", user_id=payment["user_id"]
        )


async def _activate_subscription(pool, user_id: int, plan: str, months: int) -> None:
    await pool.execute(
        """INSERT INTO subscriptions (user_id, plan, expires_at, is_active)
           VALUES ($1, $2, now() + ($3 || ' months')::INTERVAL, true)
           ON CONFLICT (user_id) DO UPDATE SET
               plan       = EXCLUDED.plan,
               is_active  = true,
               expires_at = CASE
                   WHEN subscriptions.expires_at > now()
                       THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                   ELSE now() + ($3 || ' months')::INTERVAL
               END,
               started_at = CASE
                   WHEN subscriptions.expires_at > now() THEN subscriptions.started_at
                   ELSE now()
               END""",
        user_id,
        plan,
        str(months),
    )
    try:
        from bot.utils.subscription import invalidate_plan_cache
        invalidate_plan_cache(user_id)
    except Exception:
        pass
