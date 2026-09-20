"""Крупные ответы API уходят сжатыми, поток событий — нет.

Мини-апп открывают с телефона: списки аккаунтов, каналов и ботов отдавались
целиком и без сжатия, хотя однотипный JSON сжимается примерно вдесятеро.
Статику сжимали и раньше, ответы API — нет.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from aiohttp import ClientSession, web

from services import mini_app_api as m


@contextlib.asynccontextmanager
async def serve(routes, middlewares):
    app = web.Application(middlewares=middlewares)
    for path, handler in routes:
        app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = ClientSession()
    try:
        yield session, f"http://127.0.0.1:{port}"
    finally:
        await session.close()
        await runner.cleanup()


def _compress_middleware():
    """Достать compress_middleware из setup_routes, собрав приложение на заглушке пула."""
    app = web.Application()

    class _Pool:
        async def execute(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def fetchrow(self, *a, **kw):
            return None

        async def fetchval(self, *a, **kw):
            return None

    m.setup_routes(app, _Pool())
    found = [mw for mw in app.middlewares
             if getattr(mw, "__name__", "") == "compress_middleware"]
    assert found, "compress_middleware должна вешаться на приложение"
    return found[0]


BIG = json.dumps([{"id": i, "title": "канал номер %d" % i} for i in range(2000)])
SMALL = json.dumps({"ok": True})


async def _big(request):
    return web.Response(text=BIG, content_type="application/json")


async def _small(request):
    return web.Response(text=SMALL, content_type="application/json")


async def _stream(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    await resp.write(b"data: hello\n\n")
    await resp.write_eof()
    return resp


def test_large_json_is_compressed():
    mw = _compress_middleware()

    async def _go():
        async with serve([("/api/big", _big)], [mw]) as (s, base):
            async with s.get(f"{base}/api/big",
                             headers={"Accept-Encoding": "gzip, deflate"}) as r:
                body = await r.read()
                return (r.headers.get("Content-Encoding"),
                        int(r.headers.get("Content-Length") or 0),
                        len(body), r.headers.get("Vary"))

    encoding, sent, decoded, vary = asyncio.run(_go())
    assert encoding, "крупный ответ должен уходить сжатым"
    assert decoded == len(BIG.encode()), "тело после распаковки не должно меняться"
    assert sent < decoded / 4, f"сжатие почти ничего не дало: {sent} из {decoded}"
    assert vary == "Accept-Encoding", "иначе кэши перепутают сжатый и обычный ответ"


def test_small_response_is_left_alone():
    mw = _compress_middleware()

    async def _go():
        async with serve([("/api/small", _small)], [mw]) as (s, base):
            async with s.get(f"{base}/api/small",
                             headers={"Accept-Encoding": "gzip"}) as r:
                await r.read()
                return r.headers.get("Content-Encoding")

    assert asyncio.run(_go()) is None


def test_client_without_gzip_gets_plain_body():
    mw = _compress_middleware()

    async def _go():
        async with serve([("/api/big", _big)], [mw]) as (s, base):
            async with s.get(f"{base}/api/big",
                             headers={"Accept-Encoding": "identity"}) as r:
                body = await r.read()
                return r.headers.get("Content-Encoding"), len(body)

    encoding, size = asyncio.run(_go())
    assert encoding is None
    assert size == len(BIG.encode())


def test_event_stream_is_not_buffered_by_compression():
    """SSE обязан уходить кусками: сжатие копило бы их в буфере компрессора."""
    mw = _compress_middleware()

    async def _go():
        async with serve([("/api/events", _stream)], [mw]) as (s, base):
            async with s.get(f"{base}/api/events",
                             headers={"Accept-Encoding": "gzip"}) as r:
                body = await asyncio.wait_for(r.read(), timeout=5)
                return r.headers.get("Content-Encoding"), body

    encoding, body = asyncio.run(_go())
    assert encoding is None, "поток событий сжимать нельзя"
    assert b"data: hello" in body
