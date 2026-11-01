"""
Webhook Server — HTTP endpoint для входящих webhook-уведомлений.

Запускается как asyncio-задача рядом с polling-ботом.
Порт: WEBHOOK_PORT (default 8080, Railway проксирует автоматически)

Поддерживает:
- Railway deploy webhook: POST /webhook/deploy — уведомления о деплоях

Входа CryptoPay (/webhook/cryptopay) больше нет: счетов CryptoPay продукт не
выставляет (createInvoice нигде не вызывался), а проверка в нём ждала токен
в заголовке, хотя CryptoPay подписывает тело HMAC — настоящее уведомление он не
принял бы никогда. Мёртвая денежная дверь — только поверхность атаки. История
старых оплат CryptoPay/Stars по-прежнему читается services/payment_recovery.

Универсального денежного хука (/webhook/payment) тоже больше нет. Его не звал
ни один провайдер, а при заданном WEBHOOK_SECRET тело без поля `plan` выдавало
месяц подписки за любую сумму от 90 % цены, и срок из тела не сверялся со
списком сроков. Живой путь оплаты один — services/payment_checker.py,
проверка транзакции в блокчейне. Понадобится провайдер — хук пишется под его
формат подписи и его счета, а не «универсальный».
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import asyncpg
from aiohttp import web
from aiogram import Bot

from services.logger import log_exc_swallow
from services.security import (
    check_rate_limit,
    rate_limit_response,
    escape_html,
)

log = logging.getLogger(__name__)

# Railway sets PORT env var for web services; WEBHOOK_PORT is our fallback
_WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))

def make_app(pool: asyncpg.Pool, bot: Bot) -> web.Application:
    app = web.Application()
    # Apply security middleware (rate limiting + security headers)
    from services.security import security_middleware
    app.middlewares.append(security_middleware())

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def root_redirect(request: web.Request) -> web.Response:
        """Redirect root URL to Mini App so MINI_APP_URL can be set without /miniapp/ suffix."""
        return web.HTTPFound("/miniapp/")

    async def deploy_webhook(request: web.Request) -> web.Response:
        """Railway deployment webhook — уведомление админов о деплое."""
        # Rate limit deploy webhook requests
        if not await check_rate_limit(request, max_requests=20, window=60):
            return rate_limit_response(request)

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

        status_emoji = {"SUCCESS": "✅", "FAILED": "❌", "CRASHED": "💥"}.get(
            status, "🔄"
        )
        branch = escape_html(commit.get("branch", "unknown"))
        sha = escape_html(
            commit.get("message", "")
        )  # Railway puts commit message in 'message' field
        deployment.get("id", "")[:7]  # Short deployment ID as reference

        # Try to get actual commit sha from deployment
        commit_full = commit.get("sha", "")
        if commit_full:
            commit_full[:7]

        created_at = deployment.get("createdAt", "")

        # Build notification text (HTML-escaped to prevent XSS via deployment data)
        lines = [
            f"<b>{status_emoji} Деплой {status}</b>",
            "",
            f"🏷️ <b>Проект:</b> {escape_html(project.get('name', 'Infragram'))}",
            f"🔧 <b>Сервис:</b> {escape_html(service.get('name', 'tg-manager'))}",
            f"🌍 <b>Окружение:</b> {escape_html(environment.get('name', 'production'))}",
            f"🌿 <b>Ветка:</b> <code>{branch}</code>",
        ]

        if commit_full:
            lines.append(f"🔖 <b>Коммит:</b> <code>{escape_html(commit_full[:12])}</code>")
        if sha:
            lines.append(f"📝 <b>Изменения:</b>\n<code>{sha[:500]}</code>")
        if creator:
            lines.append(f"👤 <b>Автор деплоя:</b> {escape_html(creator.get('name', '—'))}")
        if created_at:
            lines.append(f"🕐 <b>Время:</b> {escape_html(str(created_at))}")

        text = "\n".join(lines)

        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = {
            int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()
        }

        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                log_exc_swallow(
                    log, "Сбой отправки deploy-уведомления админу", admin_id=admin_id
                )

        log.info(
            "deploy_webhook: notified %d admins about deployment %s status=%s",
            len(admin_ids),
            deployment.get("id", "")[:8],
            status,
        )
        return web.Response(status=200, text="OK")

    async def deploy_health(request: web.Request) -> web.Response:
        """Health check for deploy webhook (used when configuring in Railway)."""
        return web.Response(status=200, text="deploy-ok")

    app.router.add_get("/health", health)
    app.router.add_get("/", root_redirect)        # root → /miniapp/ redirect
    app.router.add_get("/webhook/deploy", deploy_health)
    app.router.add_post("/webhook/deploy", deploy_webhook)

    from services import email_oauth, rest_api, mini_app_api, managed_bot_webhooks

    email_oauth.add_routes(app, pool, bot)
    rest_api.add_routes(app, pool, bot)

    # Telegram Mini App API + SSE real-time dashboard
    mini_app_api.setup_routes(app, pool)

    # Managed bot webhook receiver (item 5: webhooks instead of polling)
    app.router.add_post("/tgbot/hook/", managed_bot_webhooks.make_webhook_route(pool))

    return app


async def run(
    pool: asyncpg.Pool,
    bot: Bot,
    dp=None,
    webhook_path: str = "/webhook",
    http=None,
) -> None:
    """Запустить HTTP-сервер (webhook + payment + mini_app_api).

    Если передан dp — регистрирует Telegram webhook на том же порту,
    чтобы не занимать PORT дважды.
    """
    app = make_app(pool, bot)

    if dp is not None:
        try:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application as _sa
            SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                pool=pool,
                http=http,
            ).register(app, path=webhook_path)
            _sa(app, dp, bot=bot, pool=pool, http=http)
            log.info("Telegram webhook registered at %s on the shared server", webhook_path)
        except Exception:
            log.exception("Failed to register aiogram webhook handler — falling back to polling")

    # D19: строка запроса в access-логе — без значений токенов и пропусков.
    from services.secret_masking import RedactingAccessLogger
    runner = web.AppRunner(app, access_log_class=RedactingAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _WEBHOOK_PORT)
    try:
        await site.start()
        log.info("HTTP server started on port %d (webhook=%s)", _WEBHOOK_PORT, webhook_path if dp else "polling")
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("HTTP server error: %s", e)
    finally:
        await runner.cleanup()
