#!/usr/bin/env python3
"""TG Manager — Telegram bot management platform."""
import asyncio
import logging
import os
import ssl
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, CallbackQuery
from config import BOT_TOKEN
from database.db import create_pool
from services.logger import configure_root_logger, get_logger, log_exc_swallow
from bot.middlewares.user_activity import UserActivityLogMiddleware
from bot.handlers import start, bots, edit, audience, webhooks, broadcast, bulk
from bot.handlers import commands as cmd_handler
from bot.handlers import templates as tpl_handler
from bot.handlers import schedule as sch_handler
from bot.handlers import multigeo as multigeo_handler
from bot.handlers import auto_reply as ar_handler
from bot.handlers import stats as stats_handler
from bot.handlers import relay as relay_handler
from bot.handlers import funnels as funnels_handler
from bot.handlers import notes as notes_handler
from bot.handlers import swarm as swarm_handler
from bot.handlers import crm as crm_handler
from bot.handlers import experiments as experiments_handler
from bot.handlers import deeplinks as deeplinks_handler
from bot.handlers import engagement as engagement_handler
from bot.handlers import seo as seo_handler
from bot.handlers import network as network_handler
from bot.handlers import subscription as sub_handler
from bot.handlers import ai_assistant as ai_handler
from bot.handlers import net_broadcast as net_bc_handler
from bot.handlers import network_bulk as net_bulk_handler
from bot.handlers import ranking as ranking_handler
from bot.handlers import accounts as accounts_handler
from bot.handlers import referral as referral_handler
from bot.handlers import channel_ops as channel_ops_handler
from bot.handlers import admin as admin_handler
from bot.handlers import admin_users as admin_users_handler
from bot.handlers import botmother_menu as bm_handler
from bot.handlers import bot_factory as bot_factory_handler
from bot.handlers import group_factory as group_factory_handler
from bot.handlers import mass_ops as mass_ops_handler
from bot.handlers import asset_templates as asset_tpl_handler
from bot.handlers import channel_factory as chan_factory_handler
from bot.handlers import competitors as competitors_handler
from bot.handlers import mass_publish as mass_pub_handler
from bot.handlers import quick_post as quick_post_handler
from bot.handlers import global_presence as global_presence_handler
from bot.handlers import health_dashboard as health_handler
from bot.handlers import proxy_manager as proxy_handler
from bot.handlers import cluster_manager as cluster_handler
from bot.handlers import audience_parser as audience_parser_handler
from bot.handlers import account_warmup as account_warmup_handler
from bot.handlers import infra_analytics as infra_analytics_handler
from bot.handlers import account_cleaner as account_cleaner_handler
from bot.handlers import dm_campaigns as dm_campaigns_handler
from bot.handlers import strike as strike_handler
from bot.handlers import active_tasks as active_tasks_handler
from bot.handlers import topology as topology_handler
from bot.handlers import presence_pack as presence_pack_handler
from bot.handlers import approval_flow as approval_flow_handler
from bot.handlers import workspaces as workspaces_handler
from bot.handlers import error_report as error_report_handler
from services import scheduler
from services import auto_responder
from services import relay as relay_service
from services import funnel_runner
from services import payment_checker
from services import ranking_checker
from services import search_observer
from services import account_monitor
from services import trust_engine
from services import shadowban_monitor
from services import op_worker
from services import behavioral_engine
from services import account_warmer
from services import account_health
from services import payment_webhook
from services import task_registry
from services import drift_detector
from services import deploy_notifier
from services import infra_memory

configure_root_logger(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    use_json=os.environ.get("LOG_FORMAT") == "json",
)
log = get_logger(__name__)


async def _global_error_handler(event: ErrorEvent) -> None:
    """Catch any unhandled exception and show it to the user."""
    exc = event.exception

    # Silently ignore "message is not modified" — happens when refresh yields same content
    exc_str = str(exc).lower()
    if "message is not modified" in exc_str or "not modified" in exc_str:
        return

    log.exception("Unhandled error in update %s", event.update, exc_info=exc)
    update = event.update
    try:
        if update.callback_query:
            cb: CallbackQuery = update.callback_query
            try:
                await cb.answer(f"⚠️ Ошибка: {type(exc).__name__}", show_alert=True)
            except Exception:
                log_exc_swallow(log, "Failed to answer callback_query on error handler")
            try:
                exc_text = str(exc)[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await cb.message.answer(
                    f"⚠️ <b>Внутренняя ошибка</b>\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Failed to send error message via callback_query")
        elif update.message:
            try:
                exc_text = str(exc)[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await update.message.answer(
                    f"⚠️ <b>Внутренняя ошибка</b>\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Failed to send error message via message")
    except Exception:
        log_exc_swallow(log, "Double-fault in error handler")


async def main() -> None:
    bot_session = AiohttpSession()
    bot_session._connector_init["ssl"] = False

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=bot_session,
    )
    dp = Dispatcher(storage=MemoryStorage())
    activity_log_middleware = UserActivityLogMiddleware()
    dp.message.outer_middleware(activity_log_middleware)
    dp.callback_query.outer_middleware(activity_log_middleware)

    dp.include_router(bm_handler.router)
    dp.include_router(bot_factory_handler.router)
    dp.include_router(group_factory_handler.router)
    dp.include_router(mass_ops_handler.router)
    dp.include_router(asset_tpl_handler.router)
    dp.include_router(chan_factory_handler.router)
    dp.include_router(global_presence_handler.router)
    dp.include_router(quick_post_handler.router)
    dp.include_router(mass_pub_handler.router)
    dp.include_router(competitors_handler.router)
    dp.include_router(sub_handler.router)
    dp.include_router(start.router)
    dp.include_router(bots.router)
    dp.include_router(edit.router)
    dp.include_router(audience.router)
    dp.include_router(webhooks.router)
    dp.include_router(broadcast.router)
    dp.include_router(cmd_handler.router)
    dp.include_router(tpl_handler.router)
    dp.include_router(sch_handler.router)
    dp.include_router(bulk.router)
    dp.include_router(multigeo_handler.router)
    dp.include_router(ar_handler.router)
    dp.include_router(stats_handler.router)
    dp.include_router(funnels_handler.router)
    dp.include_router(notes_handler.router)
    dp.include_router(swarm_handler.router)
    dp.include_router(crm_handler.router)
    dp.include_router(experiments_handler.router)
    dp.include_router(deeplinks_handler.router)
    dp.include_router(engagement_handler.router)
    dp.include_router(seo_handler.router)
    dp.include_router(network_handler.router)
    dp.include_router(net_bulk_handler.router)
    dp.include_router(net_bc_handler.router)
    dp.include_router(ai_handler.router)
    dp.include_router(ranking_handler.router)
    dp.include_router(accounts_handler.router)
    dp.include_router(referral_handler.router)
    dp.include_router(channel_ops_handler.router)
    dp.include_router(health_handler.router)
    dp.include_router(proxy_handler.router)
    dp.include_router(cluster_handler.router)
    dp.include_router(audience_parser_handler.router)
    dp.include_router(account_warmup_handler.router)
    dp.include_router(infra_analytics_handler.router)
    dp.include_router(account_cleaner_handler.router)
    dp.include_router(topology_handler.router)
    dp.include_router(presence_pack_handler.router)
    dp.include_router(dm_campaigns_handler.router)
    dp.include_router(strike_handler.router)
    dp.include_router(active_tasks_handler.router)
    dp.include_router(workspaces_handler.router)
    dp.include_router(approval_flow_handler.router)
    dp.include_router(error_report_handler.router)
    dp.include_router(relay_handler.router)  # relay last — catches F.reply_to_message
    # admin message handler AFTER relay so FSM handlers take priority
    dp.include_router(admin_users_handler.router)
    dp.include_router(admin_handler.router)
    dp.error.register(_global_error_handler)

    pool = await create_pool()

    # Load persistent platform settings
    from database import db as _db
    from bot.utils.subscription import set_free_mode
    _fm = await _db.get_platform_setting(pool, "free_mode", "false")
    set_free_mode(_fm == "true")
    log.info("Free Mode on startup: %s", "ON" if _fm == "true" else "OFF")

    # On restart: reset any "running" operations back to "pending" so they get replayed.
    # This prevents operations from hanging forever when the bot was killed mid-execution.
    try:
        _reset_count = await pool.execute(
            "UPDATE operation_queue SET status='pending', started_at=NULL WHERE status='running'"
        )
        log.info("Startup: reset stale running operations → pending (%s)", _reset_count)
    except Exception as _e:
        log.warning("Startup: could not reset stale running operations: %s", _e)

    # Send deployment notification to admins on startup (detects new deploys)
    asyncio.create_task(deploy_notifier.notify_deploy(pool, bot))

    # Register bot commands (shows in Telegram "/" menu)
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start",        description="Главное меню"),
        BotCommand(command="menu",         description="🏠 BotMother OS"),
        BotCommand(command="ai",           description="AI-ассистент"),
        BotCommand(command="remember",     description="Запомнить факт для AI"),
        BotCommand(command="memory",       description="Показать память AI"),
        BotCommand(command="forget",       description="Удалить запись памяти"),
        BotCommand(command="accounts",     description="Мои аккаунты"),
        BotCommand(command="ops",          description="Операции с аккаунтами"),
        BotCommand(command="subscription", description="Подписка и оплата"),
        BotCommand(command="ranking",      description="Трекер позиций в поиске"),
        BotCommand(command="tasks",         description="Активные задачи (отмена)"),
        BotCommand(command="topology",     description="🗺️ Топология активов"),
        BotCommand(command="post",         description="✍️ Создать пост в каналы"),
        BotCommand(command="cancel",       description="Отменить текущее действие"),
    ])
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    http = aiohttp.ClientSession(connector=connector)
    async def _resilient(name: str, fn, *args):
        """Wrap a background service factory with auto-restart on crash.
        fn(*args) is called fresh each restart so the coroutine is never reused.
        """
        while True:
            try:
                await fn(*args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Service %s crashed: %s — restarting in 30s", name, e, exc_info=True)
                await asyncio.sleep(30)

    try:
        asyncio.create_task(_resilient("scheduler",        scheduler.run, pool, http))
        asyncio.create_task(_resilient("auto_responder",   auto_responder.run, pool, http, bot))
        asyncio.create_task(_resilient("relay",            relay_service.run, pool, http))
        asyncio.create_task(_resilient("funnel_runner",    funnel_runner.run, pool, http))
        asyncio.create_task(_resilient("payment_checker",  payment_checker.run, pool, http, bot))
        asyncio.create_task(_resilient("ranking_checker",  ranking_checker.run, pool, bot))
        asyncio.create_task(_resilient("search_observer",  search_observer.run_confirmation_loop, pool, bot))
        asyncio.create_task(_resilient("account_monitor",  account_monitor.run, pool, bot))
        asyncio.create_task(_resilient("trust_engine",     trust_engine.run, pool, bot))
        asyncio.create_task(_resilient("shadowban_monitor",shadowban_monitor.run, pool, bot))
        asyncio.create_task(_resilient("op_worker",        op_worker.run, pool, bot))
        asyncio.create_task(_resilient("behavioral_engine",behavioral_engine.run, pool, bot))
        asyncio.create_task(_resilient("account_warmer",   account_warmer.run_warmup_loop, pool))
        asyncio.create_task(_resilient("account_health",   account_health.run_health_check_loop, pool))
        asyncio.create_task(_resilient("payment_webhook",  payment_webhook.run, pool, bot))
        asyncio.create_task(_resilient("task_registry",  task_registry.run_cleanup_loop))
        asyncio.create_task(_resilient("drift_detector",  drift_detector.run, pool, bot))
        asyncio.create_task(_resilient("infra_memory",    infra_memory.run_flush_loop, pool))
        log.info("TG Manager started")
        await dp.start_polling(bot, pool=pool, http=http)
    finally:
        await pool.close()
        await http.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
