#!/usr/bin/env python3
"""Замер отклика /start и старта главной мини-аппа при сетевой задержке до БД.

Локальная база отвечает за доли миллисекунды, поэтому на ней последовательные
походы в базу не видны вовсе — а в проде каждый стоит задержку сети до БД.
Скрипт ставит между кодом и базой TCP-прокси с заданной задержкой и меряет:

  1. `pool.fetchval("select 1")` с обычным соединением asyncpg и с
     `database.lean_reset.LeanResetConnection` (лишний сброс на возврате);
  2. `cmd_start` — /start владельца с ботами (Telegram подменён);
  3. запросы, которые мини-апп шлёт при открытии главной, — по одному и все
     разом, как это делает boot().

Нужна база с накатанной схемой и данными владельца --uid (боты, аккаунты,
операции). Пример:

    DATABASE_URL=postgresql://postgres@127.0.0.1:5433/tg \\
    MANAGER_BOT_TOKEN=123:x python deploy/scripts/bench_db_latency.py --rtt 20

Замер 15.11.2026 (RTT 20 мс, 1,5 млн сообщений, 800 тыс. подписчиков):
pool.fetchval 43 → 21 мс; /start 434 → 114 мс (медиана); главная мини-аппа
целиком 935 → 260 мс.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOOT = ["/api/miniapp/dashboard", "/api/miniapp/summary",
        "/api/miniapp/networks/project_overview", "/api/miniapp/onboarding",
        "/api/miniapp/organism/pulse", "/api/miniapp/subscription",
        "/api/miniapp/referral", "/api/miniapp/bots?limit=50", "/api/miniapp/config"]


async def _latency_proxy(listen_port: int, host: str, port: int, rtt_ms: float):
    oneway = rtt_ms / 2000.0

    async def pipe(r, w):
        q: asyncio.Queue = asyncio.Queue()

        async def reader():
            while True:
                d = await r.read(65536)
                await q.put((time.monotonic() + oneway, d))
                if not d:
                    return

        async def writer():
            while True:
                t, d = await q.get()
                dt = t - time.monotonic()
                if dt > 0:
                    await asyncio.sleep(dt)
                if not d:
                    w.close()
                    return
                w.write(d)
                await w.drain()

        await asyncio.gather(reader(), writer(), return_exceptions=True)

    async def handle(cr, cw):
        sr, sw = await asyncio.open_connection(host, port)
        await asyncio.gather(pipe(cr, sw), pipe(sr, cw), return_exceptions=True)

    return await asyncio.start_server(handle, "127.0.0.1", listen_port)


def _med(xs):
    return f"{statistics.median(xs):.0f} мс"


async def main(args) -> None:
    import asyncpg
    from database.lean_reset import LeanResetConnection
    from database.pool_config import get_pool_config

    src = urlparse(os.environ["DATABASE_URL"])
    srv = await _latency_proxy(args.proxy_port, src.hostname or "127.0.0.1",
                               src.port or 5432, args.rtt)
    dsn = urlunparse(src._replace(netloc=f"{src.username or 'postgres'}@127.0.0.1:{args.proxy_port}"))
    print(f"Задержка до БД: {args.rtt:.0f} мс туда-обратно")

    for cls, label in ((asyncpg.Connection, "обычное соединение"),
                       (LeanResetConnection, "LeanResetConnection")):
        p = await asyncpg.create_pool(dsn, min_size=1, max_size=1, connection_class=cls)
        await p.fetchval("select 1")
        xs = []
        for _ in range(10):
            t = time.perf_counter()
            await p.fetchval("select 1")
            xs.append((time.perf_counter() - t) * 1000)
        print(f"  pool.fetchval, {label}: {_med(xs)}")
        await p.close()

    pool = await asyncpg.create_pool(dsn, connection_class=LeanResetConnection,
                                     **get_pool_config())
    from unittest.mock import AsyncMock, patch

    from aiogram.types import Message

    from bot.handlers import start as st

    msg = Message.model_validate({
        "message_id": 1, "date": 0, "text": "/start",
        "chat": {"id": args.uid, "type": "private"},
        "from": {"id": args.uid, "is_bot": False, "first_name": "T"}})
    xs = []
    with patch.object(Message, "answer", new=AsyncMock()):
        for i in range(args.n + 3):
            t = time.perf_counter()
            await st.cmd_start(msg, pool)
            if i >= 3:
                xs.append((time.perf_counter() - t) * 1000)
    print(f"  /start: {_med(xs)}")

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api as m
    from services.mini_app_auth import make_token

    app = web.Application()
    m.setup_routes(app, pool)
    client = TestClient(TestServer(app))
    await client.start_server()
    hdr = {"Authorization": "Bearer " + make_token(args.uid, os.environ["MANAGER_BOT_TOKEN"])}

    async def hit(path):
        t = time.perf_counter()
        r = await client.get(path, headers=hdr)
        await r.read()
        return (time.perf_counter() - t) * 1000

    per = {p: [] for p in BOOT}
    tot = []
    for i in range(args.n + 2):
        for p in BOOT:
            m._cache.clear()
            d = await hit(p)
            if i >= 2:
                per[p].append(d)
        m._cache.clear()
        t = time.perf_counter()
        await asyncio.gather(*(hit(p) for p in BOOT))
        if i >= 2:
            tot.append((time.perf_counter() - t) * 1000)
    for p, xs in per.items():
        print(f"  {p}: {_med(xs)}")
    print(f"  главная мини-аппа целиком (все запросы разом): {_med(tot)}")
    await client.close()
    await pool.close()
    srv.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rtt", type=float, default=20.0, help="задержка туда-обратно, мс")
    ap.add_argument("--uid", type=int, default=1000, help="владелец для замера")
    ap.add_argument("--n", type=int, default=10, help="повторов")
    ap.add_argument("--proxy-port", type=int, default=55432)
    logging.disable(logging.CRITICAL)
    asyncio.run(main(ap.parse_args()))
