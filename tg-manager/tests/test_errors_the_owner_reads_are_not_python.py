"""Владелец не должен получать в ответ фразу Python вместо причины.

ЧТО ЛОМАЛОСЬ. По слою мини-аппа повторяется такая форма:

    try:
        price_cents = int(d["price_cents"])   # поле из тела запроса
        prod = await _bsp.add_product(...)    # кидает ValueError по-русски
    except ValueError as e:
        return _err(str(e), 400)

`except ValueError` ловит ДВА источника. Наш — `raise ValueError("код
промокода обязателен")`, написанный для владельца. И встроенный: `int("abc")`
кидает `invalid literal for int() with base 10: 'abc'`. Второй уходит экрану
как текст ошибки, и владелец, который английского не читает (CLAUDE.md,
первая строка), видит фразу Python без имени поля и без того, что сделать.

Проверено по РЕАЛЬНОМУ пути до фикса: `sp_product_add`, `sp_delivery_add`,
`sp_promo_add` и `sp_faq_add` отвечали ровно `400 {"error": "invalid literal
for int() with base 10: 'abc'"}`.

ПОЧЕМУ НЕ ФИЛЬТР ПО ТЕКСТУ. Первая версия этой проверки отбраковывала ответ,
в котором нет кириллицы, и на негодном значении «дорого» прошла на СЛОМАННОМ
коде: Python вставляет в сообщение само значение, поэтому русский ввод делает
английскую фразу «русской». Поэтому причина пишется там, где поле ещё
известно по имени (`int_field`), а не угадывается потом по тексту ответа.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys

import pytest
from aiohttp import web

from services import mini_app_api

sys.path.insert(0, "tests")
from miniapp_source import api_files  # noqa: E402

_LATIN_PHRASE = re.compile(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}")
UID = 777000222


class _Persona(dict):
    """Персона бота-продавца, принадлежащая тому же владельцу, что и запрос."""


class _Pool:
    def __init__(self, uid: int) -> None:
        self.uid = uid

    async def fetchrow(self, q, *a):
        if "bot_sales_personas" in q:
            return _Persona(id=1, owner_id=self.uid, bot_id=None, name="п",
                            is_active=True)
        if "RETURNING" in q:
            # Вставка возвращает строку: без неё `dict(r)` в сервисе падает на
            # None, ручка отвечает 500, и положительная проверка «годное число
            # проходит» ничего не доказывала бы.
            return _Persona(id=1, owner_id=self.uid, persona_id=1)
        return None

    async def fetch(self, q, *a):
        return []

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        return "OK"


class _Request:
    def __init__(self, body: dict, match_info: dict | None = None) -> None:
        self._body = body
        self.match_info = match_info or {}
        self.query = {}
        self.rel_url = type("U", (), {"query": {}})()
        self.headers = {"Content-Type": "application/json"}
        self.method = "POST"
        self.path = "/"
        self.app = {}

    async def json(self):
        return self._body


@pytest.fixture()
def handlers(monkeypatch):
    """Ручки по РЕАЛЬНОМУ пути: собираем приложение и берём их из роутера."""
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: UID)
    app = web.Application()
    mini_app_api.setup_routes(app, _Pool(UID))
    out = {}
    for route in app.router.routes():
        name = getattr(route.handler, "__name__", "")
        if name:
            out[name] = route.handler
    return out


def _error_of(resp) -> str:
    try:
        return json.loads(resp.text or resp.body.decode()).get("error", "")
    except Exception:
        return ""


def _call(handler, body, match):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(handler(_Request(body, match)))
    finally:
        loop.close()


# Негодное число БЕЗ кириллицы: иначе Python вставит русский ввод в свою
# английскую фразу, и проверка пройдёт на сломанном коде.
BAD_NUMBERS = [
    ("sp_product_add", {"name": "Товар", "price_cents": "abc"}, {"pid": "1"},
     "Цена"),
    ("sp_delivery_add", {"name": "Курьер", "price_cents": "abc"}, {"pid": "1"},
     "Стоимость доставки"),
    ("sp_promo_add", {"code": "SALE", "percent": 10,
                      "min_total_cents": "abc"}, {"pid": "1"},
     "Минимальная сумма заказа"),
    ("sp_faq_add", {"question": "в", "answer": "о", "priority": "abc"},
     {"pid": "1"}, "Приоритет"),
]


@pytest.mark.parametrize("name,body,match,field", BAD_NUMBERS)
def test_a_bad_number_names_the_field_in_russian(handlers, name, body, match,
                                                 field):
    handler = handlers.get(name)
    assert handler is not None, f"ручка {name} не зарегистрирована"
    text = _error_of(_call(handler, body, match))
    assert text, f"{name}: ответ без текста ошибки — владельцу нечего прочесть"
    assert not _LATIN_PHRASE.search(text), (
        f"{name}: владелец получил фразу Python вместо причины: {text!r}")
    assert field in text, (
        f"{name}: ошибка не называет поле — владельцу негде искать: {text!r}")


def test_our_own_russian_message_still_reaches_the_owner(handlers):
    """Фикс не должен съесть наши собственные русские сообщения."""
    text = _error_of(_call(handlers["sp_promo_add"],
                           {"code": "", "percent": 10}, {"pid": "1"}))
    assert "промокод" in text.lower(), (
        f"наше сообщение о пустом промокоде до владельца не дошло: {text!r}")


def test_a_good_number_still_goes_through(handlers):
    """Проверка не должна проходить за счёт того, что ручка отвергает всё."""
    resp = _call(handlers["sp_faq_add"],
                 {"question": "в", "answer": "о", "priority": "3"},
                 {"pid": "1"})
    assert resp.status == 200, (
        f"годное число тоже отвергнуто ({resp.status}): "
        f"{_error_of(resp)!r} — проверка выше ничего не доказывает")


# ── Сеть по всему слою ──────────────────────────────────────────────────────
# Правило узкое и проверяемое: внутри try, чей except отдаёт экрану str(исключения),
# не должно быть ВСТРОЕННОГО преобразования данных запроса. Наши собственные
# `raise ValueError("по-русски")` под это правило не попадают, поэтому сеть не
# требует переписывать весь слой — только те места, где язык ответа зависит от
# того, кто кинул исключение.
_CONVERSIONS = {"int", "float", "Decimal", "round", "complex"}
_METHODS = {"fromisoformat", "strptime", "fromtimestamp"}
# Данные запроса: тело (`d`, `data`, `body`, `payload`) и путь/запрос.
_REQUEST_DATA = re.compile(
    r"\b(?:d|data|body|payload|j|obj)\s*(?:\[|\.get\()"
    r"|request\.(?:match_info|query|rel_url)")


def _exc_vars(arg) -> list[str]:
    """Имена, чей текст попадёт в ответ: `str(e)` и `f\"…{e}\"`."""
    out = []
    if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
            and arg.func.id == "str" and arg.args
            and isinstance(arg.args[0], ast.Name)):
        out.append(arg.args[0].id)
    if isinstance(arg, ast.JoinedStr):
        for v in arg.values:
            if not isinstance(v, ast.FormattedValue):
                continue
            inner = v.value
            if isinstance(inner, ast.Call) and inner.args:
                inner = inner.args[0]
            if isinstance(inner, ast.Name):
                out.append(inner.id)
    return out


def _forwards_exception_text(node: ast.Try) -> bool:
    for h in node.handlers:
        if not h.name:
            continue
        for n in ast.walk(h):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_err" and n.args
                    and h.name in _exc_vars(n.args[0])):
                return True
    return False


def _guarded_here(node: ast.AST) -> bool:
    """Преобразование со СВОИМ обработчиком рядом не считается: оно уже
    отвечает по-русски само (так сделан разбор даты напоминания в
    mini_app_uch)."""
    return isinstance(node, ast.Try) and bool(node.handlers)


def _raw_conversions(trees) -> list[str]:
    out = []
    for name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not _forwards_exception_text(node):
                continue
            for st in node.body:
                if _guarded_here(st):
                    continue
                for n in ast.walk(st):
                    if _guarded_here(n):
                        continue
                    bad = (isinstance(n, ast.Call)
                           and ((isinstance(n.func, ast.Name)
                                 and n.func.id in _CONVERSIONS)
                                or (isinstance(n.func, ast.Attribute)
                                    and n.func.attr in _METHODS))
                           and n.args
                           and _REQUEST_DATA.search(ast.unparse(n.args[0])))
                    if bad:
                        out.append(f"{name}:{n.lineno} {ast.unparse(n)[:70]}")
    return sorted(set(out))


def test_no_python_phrase_can_reach_the_owner():
    trees = [(p.name, ast.parse(p.read_text(encoding="utf-8")))
             for p in api_files()]
    raw = _raw_conversions(trees)
    assert not raw, (
        "встроенное преобразование данных запроса стоит в try, чей except "
        "отдаёт экрану текст исключения — владелец получит фразу Python. "
        "Разбирайте число через int_field(значение, «Имя поля»):\n  "
        + "\n  ".join(raw))


def test_the_net_would_catch_the_old_shape():
    """Самопроверка: на заведомо сломанной форме сеть обязана срабатывать."""
    broken = ("async def h(request):\n"
              "    try:\n"
              "        price = int(d['price_cents'])\n"
              "        await save(price)\n"
              "    except ValueError as e:\n"
              "        return _err(str(e), 400)\n")
    assert _raw_conversions([("пример", ast.parse(broken))]), (
        "сеть не видит ту самую форму, ради которой написана")
    fixed = broken.replace("int(d['price_cents'])",
                           "int_field(d['price_cents'], 'Цена')")
    assert not _raw_conversions([("пример", ast.parse(fixed))]), (
        "сеть ругается и на исправленную форму — так её нельзя погасить")


def test_int_field_explains_itself_in_russian():
    from services.mini_app_api import int_field

    assert int_field("5", "Цена") == 5
    assert int_field(" 7 ", "Цена") == 7
    assert int_field(None, "Приоритет", default=0) == 0
    assert int_field("", "Приоритет", default=0) == 0
    for value in ("abc", "дорого", "1.5", [], {}):
        with pytest.raises(ValueError) as exc:
            int_field(value, "Цена")
        assert "Цена" in str(exc.value), str(exc.value)
        assert not _LATIN_PHRASE.search(str(exc.value)), str(exc.value)
    with pytest.raises(ValueError) as exc:
        int_field(True, "Цена")
    assert "Цена" in str(exc.value), "логическое значение — не число"
    with pytest.raises(ValueError):
        int_field("0", "Процент", min_val=1)
    with pytest.raises(ValueError):
        int_field("101", "Процент", max_val=100)
