"""Регресс-тест: security_middleware конвертирует необработанное исключение
хендлера в чистый JSON 500 на /api/ путях (а не в HTML-страницу с трейсбеком,
на которой спотыкается мини-апп)."""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from services.security import security_middleware


async def _boom(request):
    raise ValueError("simulated handler crash")


async def _ok(request):
    return web.json_response({"ok": True})


@pytest.mark.asyncio
async def test_api_handler_exception_returns_json_500():
    mw = security_middleware()
    req = make_mocked_request("GET", "/api/miniapp/whatever")
    resp = await mw(req, _boom)
    assert resp.status == 500
    body = json.loads(resp.text)
    assert "error" in body
    assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_non_api_handler_exception_still_raises():
    mw = security_middleware()
    req = make_mocked_request("GET", "/some/html/page")
    with pytest.raises(ValueError):
        await mw(req, _boom)


@pytest.mark.asyncio
async def test_successful_handler_passes_through_with_headers():
    mw = security_middleware()
    req = make_mocked_request("GET", "/api/miniapp/ok")
    resp = await mw(req, _ok)
    assert resp.status == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_http_exception_is_not_swallowed():
    mw = security_middleware()
    req = make_mocked_request("GET", "/api/miniapp/notfound")

    async def _404(request):
        raise web.HTTPNotFound()

    with pytest.raises(web.HTTPNotFound):
        await mw(req, _404)
