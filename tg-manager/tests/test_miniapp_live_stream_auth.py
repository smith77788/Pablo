"""Живой поток мини-аппа переживал собственный токен.

`/api/miniapp/events` — SSE-поток: статистика, активность и ход операций,
которые приложение получает, пока открыто. Токен проверялся ОДИН раз, на
установке соединения, а дальше цикл крутился, пока жив сокет. Токен живёт два
часа — и соединение, открытое с ещё действующим токеном, продолжало отдавать
данные владельца и после того, как токен протух. Токен при этом едет в СТРОКЕ
ЗАПРОСА (EventSource не умеет заголовки), то есть оседает в логах доступа и в
истории браузера: срок его жизни — единственное, что ограничивает утёкшую
ссылку, и поток этот срок игнорировал.

Вторая половина — клиент. EventSource переподключается САМ и зовёт onerror на
каждой неудаче, а onerror ставил ещё один таймер переподключения. Пока сервер
недоступен, таймеры копились; на восстановлении связи каждый из них рвал уже
поднятое соединение ради нового.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from services import mini_app_api as M

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"

UID = 809202
PORT = 18932


class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return 0
    async def execute(self, q, *a): return "OK"


def _events_app(monkeypatch, uid_sequence):
    """Сервер с роутами мини-аппа; _get_uid отдаёт значения по списку."""
    state = {"i": 0}

    def _uid(request):
        i = state["i"]
        state["i"] = i + 1
        return uid_sequence[min(i, len(uid_sequence) - 1)]

    monkeypatch.setattr(M, "_get_uid", _uid)
    monkeypatch.setattr(M, "SSE_TICK_SECONDS", 0.05)
    app = web.Application()
    M.setup_routes(app, _Pool())
    return app


async def _read_stream(base: str, seconds: float) -> str:
    out = bytearray()
    async with ClientSession() as s:
        async with s.get(base + "/api/miniapp/events?token=whatever") as r:
            assert r.status == 200, f"поток не открылся: {r.status}"
            try:
                async def pump():
                    async for chunk in r.content.iter_any():
                        out.extend(chunk)
                await asyncio.wait_for(pump(), timeout=seconds)
            except asyncio.TimeoutError:
                # Поток не закрылся сам — ровно то, что чинится этим тестом.
                out.extend("\n[ПОТОК-НЕ-ЗАКРЫЛСЯ]".encode("utf-8"))
    return out.decode("utf-8", "replace")


async def _serve(app, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    try:
        return await fn(f"http://127.0.0.1:{PORT}")
    finally:
        await runner.cleanup()


def test_stream_closes_when_the_token_expires(monkeypatch):
    """Первый вызов — вход, дальше токен протух: поток обязан закрыться."""
    app = _events_app(monkeypatch, [UID, None])
    body = asyncio.run(_serve(app, lambda base: _read_stream(base, 3.0)))
    assert "[ПОТОК-НЕ-ЗАКРЫЛСЯ]" not in body, (
        "поток продолжает отдавать данные владельца с истёкшим токеном")
    assert "event: auth_expired" in body, (
        "клиенту не сказали причину — EventSource будет вечно ломиться с тем же "
        "протухшим токеном и получать 401")


def test_stream_keeps_running_while_the_token_is_valid(monkeypatch):
    """Страховка измерителя: живой токен поток не роняет."""
    app = _events_app(monkeypatch, [UID])
    body = asyncio.run(_serve(app, lambda base: _read_stream(base, 1.0)))
    assert "[ПОТОК-НЕ-ЗАКРЫЛСЯ]" in body, "поток закрылся при живом токене"
    assert "event: auth_expired" not in body
    assert "event: stats" in body, "поток не прислал даже первый снимок"


def test_anonymous_caller_gets_no_stream(monkeypatch):
    # Лимитер частоты — один на процесс и считает по адресу клиента. В полном
    # прогоне на 127.0.0.1 к этому моменту уже натикало с других тестов, и
    # анонимный запрос получал 429 вместо 401. Чистим окно, чтобы проверять
    # авторизацию, а не соседей по прогону.
    from services import security as _sec
    _sec._rate_limiter._requests.clear()
    app = _events_app(monkeypatch, [None])

    async def go(base):
        async with ClientSession() as s:
            async with s.get(base + "/api/miniapp/events") as r:
                return r.status

    assert asyncio.run(_serve(app, go)) == 401


def test_tick_is_a_named_constant():
    """Период опроса задаёт и нагрузку на базу, и скорость реакции на токен."""
    assert isinstance(M.SSE_TICK_SECONDS, (int, float)) and M.SSE_TICK_SECONDS > 0


# ── Клиент: одно запланированное переподключение, а не пачка ─────────────────

def _open_sse_js() -> str:
    src = INDEX.read_text("utf-8")
    start = src.index("function openSSE() {")
    return src[start:src.index("\n}", start) + 2]


def test_client_schedules_one_reconnect_at_a_time():
    body = _open_sse_js()
    assert "_sseTimer" in body, (
        "каждый onerror ставил новый таймер: пока сервер лежит, они копятся, "
        "а на восстановлении рвут уже поднятое соединение")
    assert "if (_sseTimer) return;" in body, "таймер ставится без проверки"
    assert "clearTimeout(_sseTimer)" in body, (
        "openSSE обязан гасить запланированное переподключение — иначе оно "
        "оборвёт то соединение, которое только что открыли")


def test_client_closes_the_dead_stream_before_reconnecting():
    """Иначе браузер переподключается параллельно с нашим таймером."""
    body = _open_sse_js()
    onerror = body[body.index("SSE.onerror"):]
    assert "SSE.close()" in onerror


def test_client_reauths_instead_of_looping_on_an_expired_token():
    body = _open_sse_js()
    assert "auth_expired" in body, "нет обработчика события об истёкшем токене"
    assert "_reAuth()" in body, "токен не обновляется — экран замрёт на старых цифрах"
    # Неудачное обновление токена не должно оставлять поток мёртвым.
    onerror = body[body.index("SSE.onerror"):]
    assert re.search(r"if \(_sseRetry === 2\) _reAuth\(\);", onerror), (
        "после _reAuth обязан оставаться запасной таймер переподключения")
