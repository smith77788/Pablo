"""Кэш ответов Mini App: повторный запрос не должен вешать соединение.

Раньше декораторы клали в кэш сам объект ``web.Response``. aiohttp помечает
отданный ответ ``_eof_sent``, и при второй отдаче ``prepare()`` возвращает
None — тело не пишется, клиент висит до таймаута. Под это попадали три главных
экрана Mini App: dashboard, bots, channels (TTL 30 с). Плюс ключ не включал
query string, поэтому вторая страница списка отдавала закэшированную первую.

Поднимаем НАСТОЯЩИЙ сервер на localhost: баг проявлялся только на реальной
отдаче ответа в сокет, на вызове хендлера напрямую его не видно.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiohttp import ClientSession, web

from services import mini_app_api as m


@pytest.fixture(autouse=True)
def _clean_cache():
    m._cache.clear()
    yield
    m._cache.clear()


@contextlib.asynccontextmanager
async def serve(handler):
    """Поднять одноручечное приложение и вернуть get(path) -> (status, json, headers)."""
    app = web.Application()
    app.router.add_get("/x", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = ClientSession()

    async def get(qs: str = ""):
        # Таймаут обязателен: до фикса второй запрос висел бесконечно.
        async with await asyncio.wait_for(
            session.get(f"http://127.0.0.1:{port}/x{qs}"), timeout=5
        ) as resp:
            body = None
            with contextlib.suppress(Exception):
                body = await resp.json()
            return resp.status, body, dict(resp.headers)

    try:
        yield get
    finally:
        await session.close()
        await runner.cleanup()


async def test_cached_user_second_request_returns_body(monkeypatch):
    monkeypatch.setattr(m, "_get_uid", lambda request: 777)
    calls = []

    @m._cached_user()
    async def handler(request):
        calls.append(1)
        return m._json_resp({"n": len(calls)})

    async with serve(handler) as get:
        assert (await get())[:2] == (200, {"n": 1})
        status, body, _ = await get()
        assert status == 200
        assert body == {"n": 1}, "второй запрос должен отдать тело из кэша"
    assert len(calls) == 1, "повторный запрос не должен доходить до хендлера"


async def test_cached_user_keeps_cors_and_content_type(monkeypatch):
    monkeypatch.setattr(m, "_get_uid", lambda request: 777)

    @m._cached_user()
    async def handler(request):
        return m._json_resp({"ok": True})

    async with serve(handler) as get:
        await get()
        status, body, headers = await get()
    assert status == 200 and body == {"ok": True}
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert headers.get("Content-Type", "").startswith("application/json")


async def test_cached_user_key_includes_query(monkeypatch):
    monkeypatch.setattr(m, "_get_uid", lambda request: 777)

    @m._cached_user()
    async def handler(request):
        return m._json_resp({"offset": request.query.get("offset", "0")})

    async with serve(handler) as get:
        assert (await get("?offset=0"))[1] == {"offset": "0"}
        assert (await get("?offset=100"))[1] == {"offset": "100"}


async def test_cached_user_isolates_users(monkeypatch):
    uid = {"v": 1}
    monkeypatch.setattr(m, "_get_uid", lambda request: uid["v"])

    @m._cached_user()
    async def handler(request):
        return m._json_resp({"uid": uid["v"]})

    async with serve(handler) as get:
        assert (await get())[1] == {"uid": 1}
        uid["v"] = 2
        assert (await get())[1] == {"uid": 2}, "чужой ответ не должен утечь из кэша"


async def test_errors_are_not_cached(monkeypatch):
    monkeypatch.setattr(m, "_get_uid", lambda request: 777)
    state = {"fail": True}

    @m._cached_user()
    async def handler(request):
        if state["fail"]:
            return m._err("boom", 500)
        return m._json_resp({"ok": True})

    async with serve(handler) as get:
        assert (await get())[0] == 500
        state["fail"] = False
        assert (await get())[0] == 200, "ошибка не должна залипать на весь TTL"


async def test_anonymous_requests_are_not_cached(monkeypatch):
    monkeypatch.setattr(m, "_get_uid", lambda request: None)
    calls = []

    @m._cached_user()
    async def handler(request):
        calls.append(1)
        return m._json_resp({"n": len(calls)})

    async with serve(handler) as get:
        await get()
        await get()
    assert len(calls) == 2
    assert not m._cache


async def test_cached_global_second_request_returns_body():
    calls = []

    @m._cached("k:test")
    async def handler(request):
        calls.append(1)
        return m._json_resp({"n": len(calls)})

    async with serve(handler) as get:
        await get()
        status, body, _ = await get()
    assert status == 200 and body == {"n": 1}
    assert len(calls) == 1


def test_cache_prune_drops_expired_and_caps_size():
    import time as _t

    now = _t.time()
    snap = (200, b"{}", "application/json", "utf-8", {})
    m._cache["old"] = (now - m._CACHE_TTL - 1, snap)
    m._cache["fresh"] = (now, snap)
    m._cache_prune(now)
    assert "old" not in m._cache and "fresh" in m._cache

    for i in range(m._CACHE_MAX_ENTRIES + 50):
        m._cache[f"k{i}"] = (now + i, snap)
    m._cache_prune(now + m._CACHE_MAX_ENTRIES + 100, max_ttl=10 ** 9)
    assert len(m._cache) <= m._CACHE_MAX_ENTRIES


def test_a_cached_handler_is_not_put_on_a_route_with_a_path_parameter():
    """Ключ кэша — «имя:uid:строка_запроса», и параметра ПУТИ в нём нет.

    Пока все три кэшируемых обработчика висят на путях без параметров, это
    безвредно. Повесить такой обработчик на `/api/miniapp/channel/{id}` —
    значит отдать владельцу под одним ключом ответы про РАЗНЫЕ объекты: он
    открыл бы второй канал и увидел данные первого. Это не утечка между
    владельцами (uid в ключе есть), а подмена объекта внутри своих же данных,
    и заметить её по экрану почти нельзя.

    Проверка на уровне маршрутов, а не текста: обёртка `functools.wraps`
    сохраняет имя, поэтому кэшируемый обработчик узнаётся по наличию
    `__wrapped__` и по тому, что он объявлен кэшируемым.
    """
    import re
    from pathlib import Path

    from aiohttp import web as _web

    from services import mini_app_api as _M

    root = Path(__file__).resolve().parents[1]
    src = (root / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    # Имена обработчиков, помеченных декоратором кэша.
    cached = set(re.findall(r"@_cached_user\(\)\s*\n\s*async def (\w+)", src))
    assert cached, "декоратор кэша перестал находиться — проверка измеряет не то"

    class _P:
        async def fetch(self, q, *a): return []
        async def fetchrow(self, q, *a): return None
        async def fetchval(self, q, *a): return None
        async def execute(self, q, *a): return "OK"

    app = _web.Application()
    _M.setup_routes(app, _P())
    offenders = []
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        name = getattr(route.handler, "__name__", "")
        if name in cached and "{" in path:
            offenders.append(f"{route.method} {path} → {name}")
    assert not offenders, (
        "кэшируемый обработчик стоит на пути с параметром, а параметра пути в "
        "ключе кэша нет — ответы про разные объекты лягут под один ключ:\n  "
        + "\n  ".join(offenders))
