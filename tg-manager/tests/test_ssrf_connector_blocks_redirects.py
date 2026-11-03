"""Скачивание по адресу пользователя не уходит во внутреннюю сеть.

Проверка resolve_url_is_public смотрела адрес только ДО запроса, а сам запрос
шёл по переадресациям и резолвил имя заново. Публичная страница с 302 на
http://169.254.169.254/ (метаданные облака) или на 127.0.0.1 читалась сервером
— а аватар/медиа/фото бота по ссылке задаёт пользователь. Соединитель
PublicOnlyConnector проверяет КАЖДОЕ соединение.

Тест падает без фикса (на обычном ClientSession переадресация проходит).
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import services.security as sec


async def _server(host: str, handler) -> TestServer:
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    srv = TestServer(app, host=host)
    await srv.start_server()
    return srv


@pytest.fixture
def only_127_0_0_1_is_internal(monkeypatch):
    # 127.0.0.2 играет «публичный» сервер, 127.0.0.1 — внутреннюю сеть.
    monkeypatch.setattr(sec, "is_internal_ip", lambda ip: ip == "127.0.0.1")


async def test_redirect_into_internal_network_is_blocked(only_127_0_0_1_is_internal):
    async def secret(_r):
        return web.Response(text="metadata-secret")

    inner = await _server("127.0.0.1", secret)

    async def bounce(_r):
        raise web.HTTPFound(f"http://127.0.0.1:{inner.port}/latest/meta-data")

    outer = await _server("127.0.0.2", bounce)
    try:
        async with sec.public_http_session() as s:
            with pytest.raises(Exception) as ei:
                async with s.get(f"http://127.0.0.2:{outer.port}/img.png") as resp:
                    await resp.read()
        assert "внутреннюю" in str(ei.value) or isinstance(
            ei.value.__cause__, sec.InternalAddressBlocked) or "InternalAddressBlocked" in repr(ei.value)
    finally:
        await outer.close()
        await inner.close()


async def test_public_address_still_works(only_127_0_0_1_is_internal):
    async def ok(_r):
        return web.Response(body=b"PNG")

    srv = await _server("127.0.0.2", ok)
    try:
        async with sec.public_http_session() as s:
            async with s.get(f"http://127.0.0.2:{srv.port}/a") as resp:
                assert await sec.read_capped(resp, 100) == b"PNG"
    finally:
        await srv.close()


async def test_literal_internal_ip_is_blocked():
    async def ok(_r):
        return web.Response(text="x")

    srv = await _server("127.0.0.1", ok)
    try:
        async with sec.public_http_session() as s:
            with pytest.raises(Exception):
                async with s.get(f"http://127.0.0.1:{srv.port}/") as resp:
                    await resp.read()
    finally:
        await srv.close()


async def test_read_capped_refuses_huge_body(only_127_0_0_1_is_internal):
    async def big(_r):
        return web.Response(body=b"x" * 5000)

    srv = await _server("127.0.0.2", big)
    try:
        async with sec.public_http_session() as s:
            async with s.get(f"http://127.0.0.2:{srv.port}/") as resp:
                with pytest.raises(ValueError):
                    await sec.read_capped(resp, 1000)
    finally:
        await srv.close()


def test_user_url_downloads_use_guarded_session():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("services/story_manager.py", "services/dm_engine.py",
                "services/profile_setter_engine.py", "services/mini_app_bots.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "public_http_session()" in src, rel


async def test_guarded_session_replaces_unguarded_real_session():
    import aiohttp
    async with aiohttp.ClientSession() as plain:
        async with sec.guarded_session(plain) as s:
            assert s is not plain
            assert isinstance(s.connector, sec.PublicOnlyConnector)
        assert not plain.closed, "чужую сессию не закрываем"
    fake = object()
    async with sec.guarded_session(fake) as s:
        assert s is fake


def test_webhook_senders_use_guarded_session():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("services/webhooks.py", "services/workflow_runtime.py"):
        assert "guarded_session(http)" in (root / rel).read_text(encoding="utf-8"), rel
