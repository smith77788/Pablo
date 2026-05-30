"""
Webhook Server — HTTP endpoint для входящих webhook-уведомлений.

Запускается как asyncio-задача рядом с polling-ботом.
Порт: WEBHOOK_PORT (default 8080, Railway проксирует автоматически)

Поддерживает:
- TON/TRON через внешние сервисы (например, tonapi.io webhooks)
- Cryptobot / CryptoPay webhooks
- Telegram Stars (через inline_query callback)
- Кастомный JSON-хук: POST /webhook/payment с {user_id, amount, currency, tx_hash}
- Railway deploy webhook: POST /webhook/deploy — уведомления о деплоях

Каждый webhook проходит проверку подписи (HMAC-SHA256) если задан WEBHOOK_SECRET.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta

import asyncpg
from aiohttp import web
from aiogram import Bot

from services.logger import log_exc_swallow

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
        log_exc_swallow(log, "Сбой записи платежа в БД — финансовые данные потеряны!", user_id=user_id, tx_ref=tx_ref)

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
        log_exc_swallow(log, "Сбой уведомления пользователя об активации подписки", user_id=user_id, plan=plan)
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

    async def deploy_webhook(request: web.Request) -> web.Response:
        """Railway deployment webhook — уведомление админов о деплое."""
        body = await request.read()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        # Railway sends deployment events with type field
        event_type = data.get("type", "")
        if event_type != "deployment":
            return web.Response(status=200, text="OK")

        deployment = data.get("deployment", {})
        if not deployment:
            return web.Response(status=200, text="OK")

        status = deployment.get("status", "")
        # Notify only on successful deployments (also notify on failed if needed)
        if status not in ("SUCCESS", "FAILED", "CRASHED"):
            return web.Response(status=200, text="OK")

        commit = deployment.get("commit", {})
        service = deployment.get("service", {})
        environment = deployment.get("environment", {})
        project = deployment.get("project", {})
        creator = deployment.get("creator", {})

        status_emoji = {"SUCCESS": "✅", "FAILED": "❌", "CRASHED": "💥"}.get(status, "🔄")
        branch = commit.get("branch", "unknown")
        sha = commit.get("message", "")  # Railway puts commit message in 'message' field
        commit_sha = deployment.get("id", "")[:7]  # Short deployment ID as reference

        # Try to get actual commit sha from deployment
        commit_full = commit.get("sha", "")
        if commit_full:
            commit_sha = commit_full[:7]

        created_at = deployment.get("createdAt", "")

        # Build notification text
        lines = [
            f"<b>{status_emoji} Деплой {status}</b>",
            "",
            f"🏷️ <b>Проект:</b> {project.get('name', 'BotMother')}",
            f"🔧 <b>Сервис:</b> {service.get('name', 'tg-manager')}",
            f"🌍 <b>Окружение:</b> {environment.get('name', 'production')}",
            f"🌿 <b>Ветка:</b> <code>{branch}</code>",
        ]

        if commit_full:
            lines.append(f"🔖 <b>Коммит:</b> <code>{commit_full[:12]}</code>")
        if sha:
            lines.append(f"📝 <b>Изменения:</b>\n<code>{sha[:500]}</code>")
        if creator:
            lines.append(f"👤 <b>Автор деплоя:</b> {creator.get('name', '—')}")
        if created_at:
            lines.append(f"🕐 <b>Время:</b> {created_at}")

        text = "\n".join(lines)

        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = {int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()}

        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                log_exc_swallow(log, "Сбой отправки deploy-уведомления админу", admin_id=admin_id)

        log.info("deploy_webhook: notified %d admins about deployment %s status=%s",
                 len(admin_ids), deployment.get("id", "")[:8], status)
        return web.Response(status=200, text="OK")

    async def deploy_health(request: web.Request) -> web.Response:
        """Health check for deploy webhook (used when configuring in Railway)."""
        return web.Response(status=200, text="deploy-ok")

    app.router.add_get("/health", health)
    app.router.add_get("/webhook/deploy", deploy_health)
    app.router.add_post("/webhook/payment", payment_webhook)
    app.router.add_post("/webhook/cryptopay", cryptopay_webhook)
    app.router.add_post("/webhook/deploy", deploy_webhook)
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


