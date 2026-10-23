"""Hermes Relay: уборка следов отключённого второго опрашивающего.

Пересылку оператору и обратную маршрутизацию ответов делает
`services/auto_responder.py` — у него же оффсет обновлений бота в базе.

Здесь когда-то был ВТОРОЙ цикл `getUpdates` по тем же токенам, со своим
оффсетом в памяти. Очередь обновлений у бота одна: чей запрос с
подтверждающим offset пришёл первым, тот и забрал сообщения, а второй их
больше не увидит — Telegram подтверждённое забывает. Цикл отключили, но
код оставался на месте и ждал, пока его кто-нибудь включит обратно.

Удалён вместе с отправкой оператору, которой пользовался только он.
Инвариант «подтверждает ровно один» закреплён в
`tests/test_one_updates_poller.py`.
"""

from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db

log = logging.getLogger(__name__)

# bot_id → последний разобранный update_id. Ничего сюда уже не пишет:
# словарь остался, чтобы уборка ниже подобрала записи, доставшиеся от
# работавшего цикла в ещё не перезапущенном процессе.
_offsets: dict[int, int] = {}


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Подобрать оффсеты ботов, у которых пересылку оператору уже выключили."""
    while True:
        try:
            bots = await db.get_bots_with_relay(pool)
            active_bot_ids = {b["bot_id"] for b in bots}
            stale = set(_offsets.keys()) - active_bot_ids
            for stale_id in stale:
                _offsets.pop(stale_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Relay cleanup error")
        await asyncio.sleep(300)
