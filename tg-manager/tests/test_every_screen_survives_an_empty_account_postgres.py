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

ВТОРАЯ ПРОВЕРКА ТЕМ ЖЕ СКАНОМ: мусор в параметрах запроса. Тот же обход 284
ручек с нечисловым, отрицательным, огромным и пустым значением там, где ждут
число, и с инъекцией/обходом пути там, где ждут текст. Так нашлись четыре
настоящих дефекта: `/uch/spotlight` (не работал ни разу), `/uch/search`
(падал на любом запросе из двух слов), `/mp/catalog` и
`/ranking/history/{keyword_id}` (`int()` без проверки) и `/parser/audience`
(`LIMIT -5` — ошибка запроса). Ни один из них юнит-тесты увидеть не могли:
заглушка пула принимает любой SQL и любые типы параметров.
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

    def __init__(self, path: str, match: dict, query: dict | None = None):
        from multidict import MultiDict
        from yarl import URL
        from aiohttp import web

        query = query or {}
        self.method = "GET"
        self.path = path
        self.match_info = match
        self.headers = {"Authorization": "Bearer t"}
        self.cookies = {}
        self.query = MultiDict(query)
        self.rel_url = URL(path).with_query(query)
        self.url = URL("https://test.local" + path).with_query(query)
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

    # Владельца подменяем ДО setup_routes, и это не мелочь. Вынесенные модули
    # (`services/mini_app_*.py`) забирают `_get_uid` из mini_app_api в своё
    # замыкание в момент регистрации маршрутов, поэтому подмена ПОСЛЕ setup на
    # них не действует: их ручки отвечают 401, скан считает это «не ошибка», и
    # проверка молча не доходит до кода 79 ручек хранилища контактов и ещё
    # девяти модулей. Ровно это и было, пока сюда не добавили проверку
    # «ни одного 401» (test_the_scan_actually_reaches_the_handlers).
    mini_app_api._get_uid = lambda request: EMPTY_OWNER
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


def test_the_scan_actually_reaches_the_handlers():
    """Обратный контроль измерителя: 401 — это «скан не дошёл до кода».

    Скан признаёт ошибкой только 5xx, поэтому ручка, ответившая «не авторизован»,
    выглядит как проверенная. Пока подмена владельца стояла ПОСЛЕ setup_routes,
    так отвечали все вынесенные модули, и зелёный прогон не значил ничего.
    """
    from services import mini_app_api

    routes = _routes()
    loop, _pool = _CTX
    mini_app_api._get_uid = lambda request: EMPTY_OWNER
    unauth = []
    for path, handler in routes:
        try:
            resp = loop.run_until_complete(handler(_Req(path, _sample(path))))
        except Exception:
            continue                      # это разбирает следующий тест
        if getattr(resp, "status", 0) == 401:
            unauth.append(path)
    assert not unauth, (
        f"{len(unauth)} ручек ответили «не авторизован» — подмена владельца до "
        "них не доехала, и скан их не проверил:\n  " + "\n  ".join(unauth[:20]))


def test_no_screen_answers_an_error_to_a_new_owner():
    from services import mini_app_api

    routes = _routes()
    loop, _pool = _CTX
    mini_app_api._get_uid = lambda request: EMPTY_OWNER
    broken: list[str] = []
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
    assert not broken, (
        "у владельца без данных эти экраны отвечают ошибкой вместо «пока ничего "
        "нет»:\n  " + "\n  ".join(broken))


# Мусор в параметрах запроса. Экран не присылает такого сам, но параметры
# приходят из адресной строки и из чужих ссылок: ручка обязана ответить
# «неверный параметр» или первой страницей, а не «внутренней ошибкой сервера».
RUBBISH = {
    "нечисловое там, где число": {
        "limit": "abc", "offset": "abc", "page": "abc", "days": "abc",
        "acc_id": "abc", "bot_id": "abc"},
    "отрицательное": {"limit": "-5", "offset": "-5", "page": "-1", "days": "-7"},
    "огромное": {"limit": "99999999999999999999", "offset": "99999999999",
                 "page": "99999999", "days": "100000"},
    "пустое": {"limit": "", "offset": "", "status": "", "q": ""},
    "инъекция и обход пути в тексте": {
        "q": "'; DROP TABLE tg_accounts; --", "status": "<script>",
        "search": "%00", "sort": "../../etc/passwd"},
}


def test_no_screen_answers_an_error_to_rubbish_in_its_parameters():
    from services import mini_app_api

    routes = _routes()
    loop, _pool = _CTX
    mini_app_api._get_uid = lambda request: EMPTY_OWNER
    broken: list[str] = []
    for case, query in RUBBISH.items():
        for path, handler in routes:
            try:
                resp = loop.run_until_complete(
                    handler(_Req(path, _sample(path), query)))
            except Exception as exc:
                broken.append(f"[{case}] {path} → исключение наружу: "
                              f"{type(exc).__name__}: {str(exc)[:90]}")
                continue
            if getattr(resp, "status", 0) >= 500:
                broken.append(f"[{case}] {path} → {resp.status}")
    assert not broken, (
        "эти экраны отвечают «внутренней ошибкой сервера» на мусор в "
        "параметрах:\n  " + "\n  ".join(broken))
