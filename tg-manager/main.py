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
from services import scheduler
from services import auto_responder
from services import relay as relay_service
from services import funnel_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


async def main() -> None:
    # Environment uses a proxy with self-signed cert — disable SSL verification.
    bot_session = AiohttpSession()
    bot_session._connector_init["ssl"] = False

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=bot_session,
    )
    dp = Dispatcher(storage=MemoryStorage())

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
    dp.include_router(relay_handler.router)  # relay last — catches F.reply_to_message

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
        logging.info("TG Manager started")
        try:
            await dp.start_polling(bot, pool=pool, http=http)
        finally:
            await pool.close()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
