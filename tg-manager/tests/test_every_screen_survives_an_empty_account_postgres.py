"""Ни один экран мини-аппа не отвечает ошибкой владельцу, у которого нет данных.

ЗАЧЕМ. Первое, что делает новый владелец — открывает приложение, где ещё
ничего нет. Ручка, которая на пустых данных кидает исключение, даёт экрану
ошибку вместо «пока ничего нет», и выглядит это как «приложение не работает».
Класс дорогой и незаметный: на базе разработчика данные есть всегда.

ПОЧЕМУ ЖИВАЯ БАЗА. Эту проверку нельзя сделать на заглушке пула, и это
проверено: заглушка, отдающая None на fetchrow, объявила сломанными четыре
ручки, у которых запрос — ungrouped-агрегат (такой в Postgres ВСЕГДА отдаёт
ровно одну строку). Попытка научить заглушку различать агрегаты дала девять
новых ложных находок: `COUNT(` внутри скалярного подзапроса не делает внешний
запрос агрегатом, а без разбора SQL это не отличить. Правду знает только
Postgres — поэтому файл требует INFRAGRAM_TEST_DSN.

КАК ЗАПУСТИТЬ (локально, ~минута):

    /usr/lib/postgresql/16/bin/pg_ctl -D /var/lib/postgresql/td -o '-p 5433' start
    su postgres -c "/usr/lib/postgresql/16/bin/createdb -p 5433 ci1"
    INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/ci1 \
        python3 -m pytest tests/test_every_screen_survives_an_empty_account_postgres.py

Владелец берётся с заведомо незанятым id, поэтому «пусто» верно и на базе, где
другие тесты уже что-то оставили: это именно новый владелец в живой системе.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

os.environ.setdefault("MINIAPP_SIGNING_KEY", "x" * 40)
os.environ.setdefault("MANAGER_BOT_TOKEN", "1:dummy")
os.environ.setdefault("DATABASE_URL", DSN or "postgresql://localhost/none")

# Владелец, которого в базе нет и не будет: «пустой аккаунт» по построению.
EMPTY_OWNER = 998877665544

# Ручки, которые по своей природе не отвечают телом и проверяются отдельно.
SKIP = {
    "/api/miniapp/events": "SSE: держит соединение открытым, а не отвечает телом",
    "/api/miniapp/live": "SSE: поток событий",
}

# Значения для параметров пути: таких записей у пустого владельца нет, и ручка
# обязана ответить «не найдено», а не упасть.
SAMPLE = {"contact_id": "00000000-0000-0000-0000-000000000001", "code": "nosuch"}


def _sample(path: str) -> dict:
    return {k: SAMPLE.get(k, "1") for k in re.findall(r"\{(\w+)[:}]", path)}


class _Req:
    """Минимальный aiohttp-подобный запрос: ручкам нужен только разбор."""

    def __init__(self, path: str, match: dict):
        from multidict import MultiDict
        from yarl import URL
        from aiohttp import web

        self.method = "GET"
        self.path = path
        self.match_info = match
        self.headers = {"Authorization": "Bearer t"}
        self.cookies = {}
        self.query = MultiDict()
        self.rel_url = URL(path)
        self.url = URL("https://test.local" + path)
        self.scheme = "https"
        self.host = "test.local"
        self.app = web.Application()
        self.remote = "127.0.0.1"
        self.content_type = "application/json"
        self.transport = None

    async def json(self):
        return {}

    async def post(self):
        from multidict import MultiDict
        return MultiDict()

    async def read(self):
        return b""

    async def text(self):
        return ""


def _collect() -> tuple[list[tuple[str, object]], object]:
    """Собрать приложение на ЖИВОМ пуле и вернуть GET-ручки мини-аппа."""
    import asyncpg
    from aiohttp import web

    from services import mini_app_api

    loop = asyncio.new_event_loop()
    # asyncpg берёт текущий цикл из asyncio, а не из аргумента: без set_event_loop
    # пул создаётся в одном цикле, а запросы идут в другом («attached to a
    # different loop»), и файл молча скипался как «нет Postgres».
    asyncio.set_event_loop(loop)
    try:
        pool = loop.run_until_complete(
            asyncpg.create_pool(DSN, min_size=1, max_size=4))
    except Exception as exc:                      # нет сервера по DSN
        loop.close()
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")

    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    routes = []
    for r in app.router.routes():
        path = r.resource.canonical
        if r.method == "GET" and path.startswith("/api/miniapp/") and path not in SKIP:
            routes.append((path, r.handler))
    return routes, (loop, pool)


_ROUTES, _CTX = (None, None)


def _routes():
    global _ROUTES, _CTX
    if _ROUTES is None:
        _ROUTES, _CTX = _collect()
    return _ROUTES


def test_the_scan_sees_the_whole_app():
    """Обратный контроль самого скана: промахнувшийся сбор ничего не стережёт."""
    assert len(_routes()) > 250, (
        f"собрано всего {len(_routes())} GET-ручек — скан промахнулся")


def test_no_screen_answers_an_error_to_a_new_owner():
    from services import mini_app_api

    routes = _routes()
    loop, _pool = _CTX
    original = mini_app_api._get_uid
    mini_app_api._get_uid = lambda request: EMPTY_OWNER
    broken: list[str] = []
    try:
        for path, handler in routes:
            try:
                resp = loop.run_until_complete(handler(_Req(path, _sample(path))))
            except Exception as exc:
                broken.append(f"{path} → исключение наружу: "
                              f"{type(exc).__name__}: {str(exc)[:90]}")
                continue
            status = getattr(resp, "status", 0)
            if status >= 500:
                broken.append(f"{path} → {status}")
    finally:
        mini_app_api._get_uid = original
    assert not broken, (
        "у владельца без данных эти экраны отвечают ошибкой вместо «пока ничего "
        "нет»:\n  " + "\n  ".join(broken))
