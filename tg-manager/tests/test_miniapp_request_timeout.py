"""Потолок времени на запрос API: зависший запрос обрывается, а не висит вечно.

У aiohttp нет таймаута на обработчик, и у продукта его не было ни в одном
слое. Запрос, зависший внутри (половинчатый прокси, который принял TCP и
молчит; блокировка в базе; внешний сервис без ответа), висел БЕСКОНЕЧНО: для
владельца — вечный спиннер в мини-аппе, для сервера — копящиеся живые
соединения и занятые корутины, то есть десяток таких запросов отнимал API у
всех остальных. Поштучные `asyncio.wait_for` вокруг вызовов Telegram это не
закрывают — они стоят там, где кто-то про них вспомнил.

Проверяется поведение middleware, а не текст исходника, плюс два факта,
без которых зелёный тест ничего не значил бы: что middleware реально
зарегистрирована в приложении и что первая в списке — действительно внешняя.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import mini_app_api as M


class _Req:
    """Минимальный запрос: middleware читает только путь, метод и тип тела."""

    def __init__(self, path: str, method: str = "GET", content_type: str = "") -> None:
        self.rel_url = type("_U", (), {"path": path})()
        self.method = method
        self.content_type = content_type


async def _slow(request):
    await asyncio.sleep(5)
    return web.Response(text="успел")


# ── Сам потолок ───────────────────────────────────────────────────────────────

async def test_hanging_request_is_cut_with_504():
    mw = M.make_request_timeout_middleware(0.05)
    resp = await mw(_Req("/api/miniapp/accounts"), _slow)
    assert resp.status == 504
    assert "Повторите" in resp.text, "владелец должен получить объяснение по-русски"


async def test_hanging_handler_is_actually_cancelled():
    """Иначе обработчик продолжал бы держать соединение и после ответа 504."""
    cancelled = asyncio.Event()

    async def _handler(request):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return web.Response()

    mw = M.make_request_timeout_middleware(0.05)
    assert (await mw(_Req("/api/miniapp/accounts"), _handler)).status == 504
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_normal_request_passes_through_untouched():
    sentinel = web.Response(text="ответ")

    async def _fast(request):
        return sentinel

    mw = M.make_request_timeout_middleware(0.05)
    assert await mw(_Req("/api/miniapp/accounts"), _fast) is sentinel


# ── Исключения, без которых потолок ломал бы работающее ───────────────────────

@pytest.mark.parametrize("path", ["/api/miniapp/events", "/miniapp/index.html",
                                  "/.well-known/assetlinks.json",
                                  "/api/miniapp/cloud/file/7/download"])
async def test_streams_static_and_cloud_download_are_exempt(path):
    """SSE-поток живёт минутами по замыслу, статику отдаёт сам aiohttp, а
    скачивание собирает куски файла ИЗ TELEGRAM — для крупного файла это
    законно дольше потолка."""
    async def _handler(request):
        await asyncio.sleep(0.2)
        return web.Response(text="поток")

    mw = M.make_request_timeout_middleware(0.05)
    assert (await mw(_Req(path), _handler)).text == "поток"


async def test_file_upload_is_exempt():
    """20 МБ с мобильной сети честно идут дольше потолка; их ограничивает
    лимит размера, а не секунды."""
    async def _handler(request):
        await asyncio.sleep(0.2)
        return web.Response(text="загружено")

    mw = M.make_request_timeout_middleware(0.05)
    req = _Req("/api/miniapp/upload", "POST", "multipart/form-data; boundary=x")
    assert (await mw(req, _handler)).text == "загружено"


# ── Значение потолка ─────────────────────────────────────────────────────────

def test_timeout_comes_from_env(monkeypatch):
    monkeypatch.setenv("MINIAPP_REQUEST_TIMEOUT", "30")
    assert M.request_timeout_seconds() == 30.0


@pytest.mark.parametrize("bad", ["", "   ", "почти", "0.5", "86400", "-1"])
def test_broken_timeout_falls_back_to_default(monkeypatch, bad):
    """0.5 положил бы весь API, 86400 равносилен отсутствию потолка."""
    monkeypatch.setenv("MINIAPP_REQUEST_TIMEOUT", bad)
    assert M.request_timeout_seconds() == M._REQUEST_TIMEOUT_DEFAULT


# ── Сторожа самой проверки ───────────────────────────────────────────────────

class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return 0
    async def execute(self, q, *a): return "OK"


def test_middleware_is_registered_as_the_outermost():
    """Определить middleware и не включить её — ровно та поломка, которую
    поведенческие тесты выше не увидят."""
    app = web.Application()
    M.setup_routes(app, _Pool())
    names = [getattr(m, "__name__", "") for m in app.middlewares]
    assert names[0] == "request_timeout_middleware", (
        f"потолок времени не самый внешний слой: {names}")


async def test_first_middleware_is_really_the_outer_one():
    """Факт про aiohttp, на котором держится проверка выше: если библиотека
    однажды развернёт порядок, «самый внешний» перестанет быть внешним."""
    order: list[str] = []

    @web.middleware
    async def first(request, handler):
        order.append("first")
        return await handler(request)

    @web.middleware
    async def second(request, handler):
        order.append("second")
        return await handler(request)

    app = web.Application(middlewares=[first, second])
    async def _ok(request):
        return web.Response(text="ok")

    app.router.add_get("/", _ok)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        await cli.get("/")
    finally:
        await cli.close()
    assert order == ["first", "second"]


async def test_db_export_is_not_exempt():
    """Экспорт идёт только из базы: он обязан оставаться под потолком, иначе
    зависшая выборка снова висит вечно."""
    mw = M.make_request_timeout_middleware(0.05)
    resp = await mw(_Req("/api/miniapp/accounts/export"), _slow)
    assert resp.status == 504
