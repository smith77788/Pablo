#!/usr/bin/env python3
"""TG Manager — Telegram bot management platform."""
import asyncio
import logging
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
from bot.handlers import admin as admin_handler
from services import scheduler
from services import auto_responder
from services import relay as relay_service
from services import funnel_runner
from services import payment_checker
from services import ranking_checker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


async def _global_error_handler(event: ErrorEvent) -> None:
    """Catch any unhandled exception and show it to the user."""
    exc = event.exception
    log.exception("Unhandled error in update %s", event.update, exc_info=exc)
    update = event.update
    try:
        if update.callback_query:
            cb: CallbackQuery = update.callback_query
            try:
                await cb.answer(f"⚠️ Ошибка: {type(exc).__name__}", show_alert=True)
            except Exception:
                pass
            try:
                exc_text = str(exc)[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await cb.message.answer(
                    f"⚠️ <b>Внутренняя ошибка</b>\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        elif update.message:
            try:
                exc_text = str(exc)[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await update.message.answer(
                    f"⚠️ <b>Внутренняя ошибка</b>\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception:
        pass


async def main() -> None:
    bot_session = AiohttpSession()
    bot_session._connector_init["ssl"] = False

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=bot_session,
    )
    dp = Dispatcher(storage=MemoryStorage())

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
    dp.include_router(relay_handler.router)  # relay last — catches F.reply_to_message
    # admin message handler AFTER relay so FSM handlers take priority
    dp.include_router(admin_handler.router)
    dp.error.register(_global_error_handler)

    pool = await create_pool()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as http:
        asyncio.create_task(scheduler.run(pool, http))
        asyncio.create_task(auto_responder.run(pool, http))
        asyncio.create_task(relay_service.run(pool, http))
        asyncio.create_task(funnel_runner.run(pool, http))
        asyncio.create_task(payment_checker.run(pool, http, bot))
        asyncio.create_task(ranking_checker.run(pool, bot))
        log.info("TG Manager started")
        try:
            await dp.start_polling(bot, pool=pool, http=http)
        finally:
            await pool.close()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
