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


def _csp_directives(csp: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in csp.split(";"):
        toks = part.split()
        if toks:
            out[toks[0]] = toks[1:]
    return out


async def _html(request):
    return web.Response(text="<html><body>ok</body></html>",
                        content_type="text/html")


@pytest.mark.asyncio
async def test_csp_allows_data_uri_images_for_qr():
    """QR-код входа приходит как data:image/png;base64 в <img>.

    Без data: в img-src браузер молча блокирует картинку: код сгенерирован, но
    НЕ ВИДЕН — ровно та жалоба, ради которой это правится. И это не наследование
    от default-src: 'self' схему data: не покрывает, нужен явный img-src.
    """
    mw = security_middleware()
    req = make_mocked_request("GET", "/miniapp/")
    resp = await mw(req, _html)

    csp = resp.headers.get("Content-Security-Policy", "")
    assert csp, "CSP на HTML-ответе не выставлен"
    directives = _csp_directives(csp)
    assert "img-src" in directives, (
        "нет явного img-src — img наследует default-src 'self', а 'self' не "
        "покрывает data:, и QR-картинка блокируется")
    assert "data:" in directives["img-src"], (
        "data: не разрешён в img-src — QR-код не отобразится")


@pytest.mark.asyncio
async def test_csp_still_restricts_default_src():
    """Послабление касается только картинок — общий периметр не трогаем."""
    mw = security_middleware()
    req = make_mocked_request("GET", "/miniapp/")
    resp = await mw(req, _html)
    directives = _csp_directives(resp.headers["Content-Security-Policy"])
    assert directives.get("default-src") == ["'self'"], (
        "default-src разошёлся с 'self' — послабление шире, чем нужно для QR")
    # data: не должен утечь в script-src — иначе это дыра исполнения кода.
    assert "data:" not in directives.get("script-src", []), (
        "data: в script-src — это исполнение произвольного кода, не картинка")
