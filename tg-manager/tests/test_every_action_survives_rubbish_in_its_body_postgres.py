"""Ни одно действие мини-аппа не отвечает «внутренней ошибкой» на мусор в теле.

ЗАЧЕМ. У мини-аппа 417 изменяющих ручек (POST/PUT/DELETE/PATCH), и у каждой
контракт один: ответ — JSON, текст ошибки — по-русски. Ручка, у которой на
неожидаемом значении в теле падает `int()` или `.strip()`, этот контракт
ломает: владелец видит «внутренняя ошибка сервера» (а при исключении, выпавшем
наружу, — английскую страницу aiohttp) там, где должно стоять «неверный
идентификатор» или «нужен текст». Тело приходит с фронта, а фронт — монолит на
16 тысяч строк: пустое поле ввода, старый клиент после правки экрана и
незаполненная форма дают ровно такие значения.

ЧТО НАШЁЛ ЭТОТ ПРОБОЙ (первым прогоном, 16 настоящих дефектов):

* запись CRM шла в базу без проверки, что контакт существует И принадлежит
  владельцу: удалённый контакт давал 500 вместо «контакт не найден», а по
  ЧУЖОМУ id можно было завести строку CRM и журнал активности — внешний ключ
  проверяет только наличие контакта, не владельца;
* `int()` без проверки в телах: `uch/filter/preview`, `uch/filter/exclude`,
  `warmup`, `ghost`;
* девять ручек, где исключение выпадало НАРУЖУ обработчика (`(x or '').strip()`
  на числе, обход списка, собранного из числа): adhoc_send, network_broadcast,
  расписание бота, шаблоны, кампании ЛС, подключение найденных ботов, отправка
  в диалог, ответ в хранилище.

ПОЧЕМУ ЖИВАЯ БАЗА. Заглушка пула принимает любой SQL и любые типы параметров:
ни ошибку связывания (`DataError`), ни нарушение внешнего ключа, ни синтаксис
она не видит. Нужен Postgres — как запускать, см. docstring
tests/test_every_screen_survives_an_empty_account_postgres.py.

ВЛАДЕЛЕЦ ЗДЕСЬ НАСТОЯЩИЙ (строка в platform_users), в отличие от скана
GET-ручек: изменяющая ручка пишет строки, и у половины таблиц внешний ключ на
владельца. На выдуманном id половина находок была бы ложной — это проверено:
`/api/miniapp/workspace` падал именно так.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

os.environ.setdefault("MINIAPP_SIGNING_KEY", "x" * 40)
os.environ.setdefault("MANAGER_BOT_TOKEN", "1:dummy")
os.environ.setdefault("DATABASE_URL", DSN or "postgresql://localhost/none")

# Владелец только для этого файла: у него есть строка в platform_users и нет
# ни аккаунтов, ни ботов, ни контактов. Значит ни одно действие не дойдёт до
# Telegram — дойти там не до чего.
ACTION_OWNER = 998877665545

SAMPLE = {"contact_id": "00000000-0000-0000-0000-000000000001", "code": "nosuch"}

# Значения, которые фронт присылает, когда поле не заполнено или заполнено не
# тем. Ключи намеренно общие: какая ручка какой прочитает — решает она сама.
RUBBISH = {
    "пустое тело": {},
    "нечисловое там, где число": {
        "limit": "abc", "id": "abc", "account_id": "abc", "bot_id": "abc",
        "days": "abc", "count": "abc"},
    "отрицательное": {"limit": -5, "count": -1, "days": -7, "delay": -1},
    "не тот тип": {"account_ids": "не список", "text": 123, "ids": {},
                   "usernames": 5, "enabled": "ага"},
    "инъекция и обход пути в тексте": {
        "text": "'; DROP TABLE tg_accounts; --", "name": "<script>",
        "query": "%00"},
}


def _sample(path: str) -> dict:
    return {k: SAMPLE.get(k, "1") for k in re.findall(r"\{(\w+)[:}]", path)}


class _Req:
    """Минимальный aiohttp-подобный запрос с телом."""

    def __init__(self, path: str, match: dict, body: dict):
        from aiohttp import web
        from multidict import MultiDict
        from yarl import URL

        self._body = body
        self.method = "POST"
        self.path = path
        self.match_info = match
        self.headers = {"Authorization": "Bearer t",
                        "Content-Type": "application/json"}
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
        self.can_read_body = True
        self.body_exists = True
        self.content_length = len(json.dumps(body))

    async def json(self):
        return self._body

    async def post(self):
        from multidict import MultiDict
        return MultiDict()

    async def read(self):
        return json.dumps(self._body).encode()

    async def text(self):
        return json.dumps(self._body)

    async def multipart(self):
        raise ValueError("тело не multipart")


_STATE = {}


def _routes():
    """Собрать приложение на живом пуле и вернуть изменяющие ручки мини-аппа."""
    if _STATE:
        return _STATE["routes"]

    import asyncpg
    from aiohttp import web

    from services import mini_app_api

    loop = asyncio.new_event_loop()
    # asyncpg берёт текущий цикл из asyncio, а не из аргумента.
    asyncio.set_event_loop(loop)
    try:
        pool = loop.run_until_complete(
            asyncpg.create_pool(DSN, min_size=1, max_size=4))
    except Exception as exc:
        loop.close()
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")

    loop.run_until_complete(pool.execute(
        "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
        ACTION_OWNER))

    # Подмена владельца — ДО setup_routes: вынесенные модули забирают `_get_uid`
    # в замыкание при регистрации маршрутов, и подмена после setup на них не
    # действует (их ручки отвечали бы 401, а скан считает ошибкой только 5xx).
    mini_app_api._get_uid = lambda request: ACTION_OWNER
    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    routes = [(r.resource.canonical, r.handler) for r in app.router.routes()
              if r.method in ("POST", "PUT", "DELETE", "PATCH")
              and r.resource.canonical.startswith("/api/miniapp/")]
    _STATE.update(routes=routes, loop=loop, pool=pool)
    return routes


def test_the_scan_sees_the_whole_app():
    """Обратный контроль скана: промахнувшийся сбор ничего не стережёт."""
    assert len(_routes()) > 350, (
        f"собрано всего {len(_routes())} изменяющих ручек — скан промахнулся")


def test_the_scan_actually_reaches_the_handlers():
    """401 означает «скан не дошёл до кода», а не «ошибки нет»."""
    routes = _routes()
    loop = _STATE["loop"]
    unauth = []
    for path, handler in routes:
        try:
            resp = loop.run_until_complete(handler(_Req(path, _sample(path), {})))
        except Exception:
            continue
        if getattr(resp, "status", 0) == 401:
            unauth.append(path)
    assert not unauth, (
        f"{len(unauth)} ручек ответили «не авторизован» — подмена владельца до "
        "них не доехала:\n  " + "\n  ".join(unauth[:20]))


def test_no_action_answers_an_internal_error_to_rubbish_in_its_body():
    routes = _routes()
    loop = _STATE["loop"]
    broken: list[str] = []
    for case, body in RUBBISH.items():
        for path, handler in routes:
            try:
                resp = loop.run_until_complete(
                    handler(_Req(path, _sample(path), dict(body))))
            except Exception as exc:
                # Исключение НАРУЖУ обработчика: страховочная middleware
                # превратит его в русскую 500, но это всё равно «внутренняя
                # ошибка» там, где нужен внятный отказ.
                broken.append(f"[{case}] {path} → исключение наружу: "
                              f"{type(exc).__name__}: {str(exc)[:90]}")
                continue
            if getattr(resp, "status", 0) >= 500:
                broken.append(f"[{case}] {path} → {resp.status}")
    assert not broken, (
        "эти действия отвечают «внутренней ошибкой сервера» на мусор в теле "
        "запроса:\n  " + "\n  ".join(broken))


def test_an_exception_that_escapes_a_handler_becomes_a_russian_error():
    """Страховка слоем: исключение наружу — русский JSON, а не страница aiohttp.

    Чинить это по месту в девяти ручках значит оставить класс открытым для
    десятой. Поэтому страховка стоит middleware — и её проверяем отдельно,
    потому что обход выше зовёт обработчики напрямую, минуя её.
    """
    from aiohttp import web

    from services import mini_app_api

    app = web.Application()
    mini_app_api.setup_routes(app, _STATE["pool"])
    names = [getattr(m, "__name__", "") for m in app.middlewares]
    assert "unhandled_error_middleware" in names, (
        "страховка от необработанного исключения не установлена: "
        f"{names}")

    mw = next(m for m in app.middlewares
              if getattr(m, "__name__", "") == "unhandled_error_middleware")

    async def boom(request):
        raise RuntimeError("что-то пошло не так")

    resp = _STATE["loop"].run_until_complete(mw(_Req("/api/miniapp/x", {}, {}), boom))
    assert resp.status == 500
    body = json.loads(resp.body.decode())
    assert "error" in body, f"ответ без поля error: {body}"
    assert re.search(r"[а-яА-Я]", body["error"]), (
        f"текст ошибки не по-русски: {body['error']}")

    # Намеренный HTTP-ответ (редирект, 404 статики) страховка не перехватывает.
    async def redirect(request):
        raise web.HTTPFound("/")

    with pytest.raises(web.HTTPFound):
        _STATE["loop"].run_until_complete(
            mw(_Req("/api/miniapp/x", {}, {}), redirect))
