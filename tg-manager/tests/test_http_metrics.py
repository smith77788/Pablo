"""Время ответа и исходы ручек мини-аппа попадают в метрики (services/http_metrics).

Метка route — ШАБЛОН пути: id в пути не плодит строк метрики. Ошибка ручки
(исключение, HTTP-исключение) считается тем статусом, что увидел человек.
Поток событий в счёт не идёт. Медленный ответ — предупреждение в лог.
"""
from __future__ import annotations

import asyncio
import inspect
import logging

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import http_metrics, metrics


async def _client():
    async def ok(request):
        return web.json_response({"ok": True})

    async def boom(request):
        raise RuntimeError("сбой")

    async def missing(request):
        raise web.HTTPNotFound()

    async def slow(request):
        await asyncio.sleep(0.05)
        return web.Response(text="ok")

    app = web.Application(middlewares=[http_metrics.make_middleware()])
    app.router.add_get("/api/miniapp/bots/{bot_id}", ok)
    app.router.add_get("/api/miniapp/boom", boom)
    app.router.add_get("/api/miniapp/missing", missing)
    app.router.add_get("/api/miniapp/slow", slow)
    app.router.add_get("/api/miniapp/events", ok)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_requests_are_counted_by_route_template_and_status(monkeypatch, caplog):
    metrics.reset()
    http_metrics._window_max.clear()
    monkeypatch.setattr(http_metrics, "SLOW_SEC", 0.03)
    caplog.set_level(logging.WARNING, logger="services.http_metrics")
    client = await _client()
    try:
        for bot_id in (1, 2, 3):
            assert (await client.get(f"/api/miniapp/bots/{bot_id}")).status == 200
        assert (await client.get("/api/miniapp/boom")).status == 500
        assert (await client.get("/api/miniapp/missing")).status == 404
        assert (await client.get("/api/miniapp/slow")).status == 200
        assert (await client.get("/api/miniapp/events")).status == 200
    finally:
        await client.close()

    c = metrics.snapshot()["counters"]
    key = 'infragram_http_requests_total{method="GET",route="/api/miniapp/bots/{bot_id}",status="2xx"}'
    assert c[key] == 3, "один шаблон пути — одна строка метрики"
    assert c['infragram_http_requests_total{method="GET",route="/api/miniapp/boom",status="5xx"}'] == 1
    assert c['infragram_http_requests_total{method="GET",route="/api/miniapp/missing",status="4xx"}'] == 1
    assert not [k for k in c if "events" in k], "поток событий в счёт не идёт"
    g = metrics.snapshot()["gauges"]
    assert g['infragram_http_request_max_seconds{method="GET",route="/api/miniapp/slow"}'] >= 0.05
    assert any("медленный ответ GET /api/miniapp/slow" in r.getMessage() for r in caplog.records)


def test_window_max_resets_each_minute():
    metrics.reset()
    http_metrics._window_max.clear()
    http_metrics.record("/r", "GET", 200, 5.0, now=1000.0)
    http_metrics.record("/r", "GET", 200, 1.0, now=1030.0)
    assert metrics.snapshot()["gauges"]['infragram_http_request_max_seconds{method="GET",route="/r"}'] == 5.0
    http_metrics.record("/r", "GET", 200, 1.0, now=1061.0)
    assert metrics.snapshot()["gauges"]['infragram_http_request_max_seconds{method="GET",route="/r"}'] == 1.0


def test_middleware_is_outermost_in_miniapp_and_metrics_described():
    from services import mini_app_api
    src = inspect.getsource(mini_app_api.setup_routes)
    i_metrics = src.index("_http_metrics.make_middleware()")
    i_timeout = src.index("make_request_timeout_middleware(request_timeout_seconds())")
    assert i_metrics < i_timeout, "метрики должны видеть и 504 по потолку времени"
    for name in ("infragram_http_requests_total", "infragram_http_request_seconds_sum",
                 "infragram_http_request_max_seconds"):
        assert name in metrics._HELP
