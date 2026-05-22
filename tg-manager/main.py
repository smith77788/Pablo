#!/usr/bin/env python3
"""TG Manager — Telegram bot management platform."""
import asyncio
import logging
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from config import BOT_TOKEN
from database.db import create_pool
from bot.handlers import start, bots, edit, audience, webhooks, broadcast, bulk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start.router)
    dp.include_router(bots.router)
    dp.include_router(edit.router)
    dp.include_router(audience.router)
    dp.include_router(webhooks.router)
    dp.include_router(broadcast.router)
    dp.include_router(bulk.router)

    pool = await create_pool()
    async with aiohttp.ClientSession() as http:
        logging.info("TG Manager started")
        try:
            await dp.start_polling(bot, pool=pool, http=http)
        finally:
            await pool.close()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
