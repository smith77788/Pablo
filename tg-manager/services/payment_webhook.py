"""
Payment Webhook Server — HTTP endpoint для входящих webhook-уведомлений о платежах.

Запускается как asyncio-задача рядом с polling-ботом.
Порт: WEBHOOK_PORT (default 8080, Railway проксирует автоматически)

Поддерживает:
- TON/TRON через внешние сервисы (например, tonapi.io webhooks)
- Cryptobot / CryptoPay webhooks
- Telegram Stars (через inline_query callback)
- Кастомный JSON-хук: POST /webhook/payment с {user_id, amount, currency, tx_hash}

Каждый webhook проходит проверку подписи (HMAC-SHA256) если задан WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta

import asyncpg
from aiohttp import web
from aiogram import Bot

log = logging.getLogger(__name__)

_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
_WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

_PLAN_MAP = {
    "starter_1m": ("starter", 1),
    "starter_3m": ("starter", 3),
    "pro_1m": ("pro", 1),
    "pro_3m": ("pro", 3),
    "pro_6m": ("pro", 6),
    "enterprise_1m": ("enterprise", 1),
    "enterprise_6m": ("enterprise", 6),
    "enterprise_12m": ("enterprise", 12),
}


def _verify_signature(body: bytes, signature: str) -> bool:
    """Проверить HMAC-SHA256 подпись хука."""
    if not _WEBHOOK_SECRET:
        return True
    expected = hmac.new(
        _WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


async def _activate_subscription(
    pool: asyncpg.Pool,
    bot: Bot,
    user_id: int,
    plan: str,
    months: int,
    tx_ref: str,
    currency: str,
    amount: float,
) -> None:
    """Активировать подписку после успешного платежа."""
    expires = datetime.utcnow() + timedelta(days=30 * months)
    await pool.execute(
        """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
           VALUES($1,$2,$3,true)
           ON CONFLICT(user_id) DO UPDATE
           SET plan=$2, expires_at=$3, is_active=true, updated_at=now()""",
        user_id, plan, expires,
    )
    # Записать платёж
    try:
        await pool.execute(
            """INSERT INTO payments(user_id, amount_usd, currency, tx_hash, plan, period_months, status)
               VALUES($1,$2,$3,$4,$5,$6,'confirmed')
               ON CONFLICT DO NOTHING""",
            user_id, amount, currency, tx_ref, plan, months,
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Подписка <b>{plan.upper()}</b> активирована на {months} мес.\n"
            f"Действует до: <b>{expires.strftime('%d.%m.%Y')}</b>\n\n"
            f"Ref: <code>{tx_ref}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    log.info("payment_webhook: activated %s %s/%dm user=%d", currency, plan, months, user_id)


def make_app(pool: asyncpg.Pool, bot: Bot) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def payment_webhook(request: web.Request) -> web.Response:
        """Универсальный webhook для платёжных систем."""
        body = await request.read()
        sig = request.headers.get("X-Webhook-Signature", "")
        if _WEBHOOK_SECRET and not _verify_signature(body, sig):
            log.warning("payment_webhook: invalid signature")
            return web.Response(status=403, text="Invalid signature")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        # Извлечь поля из webhook-тела
        user_id = data.get("user_id") or data.get("payload", {}).get("user_id")
        amount = float(data.get("amount", 0))
        currency = (data.get("currency") or data.get("asset", "TON")).upper()
        tx_ref = data.get("hash") or data.get("tx_hash") or data.get("invoice_id") or "wh"
        plan_key = data.get("plan") or data.get("description", "")

        if not user_id:
            return web.Response(status=400, text="Missing user_id")

        user_id = int(user_id)

        # Попытаться определить план из description/plan поля
        plan, months = None, None
        for key, (p, m) in _PLAN_MAP.items():
            if key in plan_key.lower():
                plan, months = p, m
                break

        if not plan:
            # Fallback: определить по сумме в USD
            from config import PLAN_PRICES_USD
            prices_sorted = sorted(PLAN_PRICES_USD.items(), key=lambda x: x[1])
            for p, price_usd in prices_sorted:
                if amount >= price_usd * 0.9:
                    plan, months = p, 1

        if not plan:
            log.warning("payment_webhook: can't determine plan from %s", data)
            return web.Response(status=200, text="OK")

        await _activate_subscription(pool, bot, user_id, plan, months, tx_ref, currency, amount)
        return web.Response(status=200, text="OK")

    async def cryptopay_webhook(request: web.Request) -> web.Response:
        """Специфический обработчик для CryptoPay/CryptoBotAPI."""
        body = await request.read()
        api_token = os.getenv("CRYPTOPAY_TOKEN", "")
        if api_token:
            sig = request.headers.get("Crypto-Pay-Api-Token", "")
            if sig != api_token:
                return web.Response(status=403, text="Forbidden")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        update_type = data.get("update_type", "")
        if update_type != "invoice_paid":
            return web.Response(status=200, text="OK")

        invoice = data.get("payload", {})
        payload = invoice.get("payload", "")  # user_id:plan:months
        status = invoice.get("status", "")
        if status != "paid":
            return web.Response(status=200, text="OK")

        try:
            parts = payload.split(":")
            user_id = int(parts[0])
            plan = parts[1] if len(parts) > 1 else ""
            months = int(parts[2]) if len(parts) > 2 else 1
        except (ValueError, IndexError):
            log.warning("cryptopay_webhook: bad payload %s", payload)
            return web.Response(status=200, text="OK")

        amount = float(invoice.get("paid_usd_amount", 0))
        currency = invoice.get("asset", "USDT")
        tx_ref = str(invoice.get("invoice_id", "cp"))

        if plan not in ("starter", "pro", "enterprise"):
            return web.Response(status=200, text="OK")

        await _activate_subscription(pool, bot, user_id, plan, months, tx_ref, currency, amount)
        return web.Response(status=200, text="OK")

    app.router.add_get("/health", health)
    app.router.add_post("/webhook/payment", payment_webhook)
    app.router.add_post("/webhook/cryptopay", cryptopay_webhook)
    return app


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Запустить HTTP webhook-сервер. Вызывается как asyncio.create_task."""
    app = make_app(pool, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _WEBHOOK_PORT)
    try:
        await site.start()
        log.info("Payment webhook server started on port %d", _WEBHOOK_PORT)
        # Держать живым
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        log.exception("Payment webhook server error: %s", e)
    finally:
        await runner.cleanup()


import asyncio  # noqa: E402
