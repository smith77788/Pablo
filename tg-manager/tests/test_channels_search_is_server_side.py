"""Поиск по каналам искал только среди загруженных страниц.

Экран каналов ходит за данными страницами по 500. Поиск и срезы
(каналы/группы/мои/чужие) при этом считались НА КЛИЕНТЕ, по тому, что уже
приехало. У кого каналов больше страницы — а это продукт, который создаёт
каналы пачками, — поиск по каналу со второй страницы не находил ничего, и
человек делал единственный возможный вывод: канала нет.

Счётчики срезов врали так же: чип «👑 Мои 500» показывал не сколько их, а
сколько успело загрузиться, и стоял рядом с заголовком, где было настоящее
общее число. Тот же класс, что уже чинили в этом же экране для массовых
действий: «Все» брало загруженную страницу, а подтверждение обещало полное
число.

Теперь и поиск, и срез, и счётчики считает запрос.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
UID = 809505


class _Pool:
    """Пул, запоминающий каждый запрос и его параметры."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return {"all_n": 900, "group_n": 300, "channel_n": 600,
                "mine_n": 700, "foreign_n": 200}

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return 900

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query):
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        # Ответ кэшируется по uid И строке запроса: без query_string все вызовы
        # в тесте попали бы в один ключ, и второй получил бы ответ первого.
        self.query_string = urlencode(query)


def _handler(pool):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if route.method == "GET" and path == "/api/miniapp/channels":
            return route.handler
    raise AssertionError("роут /api/miniapp/channels не зарегистрирован")


def _call(pool, **query):
    return asyncio.run(_handler(pool)(_Req(**query)))


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    # Кэш ответов живёт в модуле и переживает тест: чистим, чтобы каждый
    # проверял свой запрос, а не повтор чужого.
    M._cache.clear()
    yield
    M._cache.clear()


def _page_query(pool) -> tuple[str, tuple]:
    for q, a in pool.calls:
        if "ORDER BY member_count" in q:
            return q, a
    raise AssertionError("запроса страницы не было")


def test_search_goes_into_the_query_not_the_client():
    pool = _Pool()
    _call(pool, search="Новости")
    q, args = _page_query(pool)
    assert "ILIKE" in q, "поиск снова считается по загруженному"
    assert "%Новости%" in args, "строка поиска не передана параметром"
    assert "Новости" not in q, "строка поиска вклеена в SQL, а не связана"


def test_search_covers_title_username_and_id():
    """Человек ищет и по названию, и по @имени, и по идентификатору."""
    pool = _Pool()
    _call(pool, search="abc")
    q, _ = _page_query(pool)
    assert "title ILIKE" in q and "username ILIKE" in q and "channel_id::text ILIKE" in q


def test_kind_and_role_filter_in_sql():
    pool = _Pool()
    _call(pool, kind="group", role="mine")
    q, _ = _page_query(pool)
    assert "type IN ('supergroup','group','chat')" in q
    assert "is_creator IS TRUE" in q


def test_unknown_filter_values_are_ignored():
    """Значение среза приходит из адреса — в запрос попадает только известное."""
    pool = _Pool()
    _call(pool, kind="'; DROP TABLE managed_channels; --", role="кто-нибудь")
    q, _ = _page_query(pool)
    assert "DROP TABLE" not in q
    # Условия среза приклеиваются к WHERE через " AND "; сами поля is_creator
    # и is_admin есть и в списке колонок — проверяем именно приклеенное.
    assert " AND is_creator IS TRUE" not in q
    assert " AND is_admin IS FALSE" not in q
    assert " AND type IN" not in q and " AND NOT type IN" not in q


def test_search_length_is_capped():
    pool = _Pool()
    _call(pool, search="я" * 500)
    _, args = _page_query(pool)
    assert any(isinstance(a, str) and len(a) <= 102 for a in args), (
        "длина строки поиска не ограничена")


def test_counts_come_from_the_server_not_the_page():
    pool = _Pool()
    d = _body(_call(pool))
    assert d["counts"] == {"all": 900, "channel": 600, "group": 300,
                           "mine": 700, "foreign": 200}, (
        "счётчики чипов обязаны считаться по всем видимым каналам")


def test_total_respects_the_active_filter():
    """По total считается «загрузить ещё» — он должен быть про тот же срез."""
    pool = _Pool()
    _call(pool, kind="group")
    totals = [q for q, _ in pool.calls if "COUNT(DISTINCT channel_id)" in q
              and "FILTER" not in q]
    assert totals, "запроса общего числа не было"
    assert "type IN ('supergroup','group','chat')" in totals[0]


def test_visibility_condition_has_one_source():
    """Условие видимости жило в файле дважды и начинало расходиться."""
    src = Path(M.__file__).read_text("utf-8")
    assert src.count("_CHANNELS_VISIBLE_SQL = ") == 1
    assert src.count("ecosystem_members em2") == 1, (
        "условие видимости снова размножено по файлу")


# ── Мини-апп ─────────────────────────────────────────────────────────────────

def _fn(name: str) -> str:
    src = INDEX.read_text("utf-8")
    for prefix in (f"\nasync function {name}(", f"\nfunction {name}("):
        i = src.find(prefix)
        if i >= 0:
            return src[i:src.index("\n}", i) + 2]
    raise AssertionError(f"функция {name} не найдена")


def test_client_sends_search_and_filter_to_the_server():
    body = _fn("_chQuery")
    assert "set('search'" in body and "set('kind'" in body and "set('role'" in body


def test_client_no_longer_filters_the_loaded_page():
    body = _fn("_chFiltered")
    assert "includes(" not in body, "поиск снова считается по загруженному"
    assert "CH_FILTER" not in body, "срез снова применяется на клиенте"


def test_search_and_chips_reload_from_the_server():
    assert "reloadChannels" in _fn("onChSearch")
    assert "reloadChannels" in _fn("filterChannels")
    assert "reloadChannels" in _fn("resetChFilters")


def test_chips_use_server_counts():
    body = _fn("renderChFilterChips")
    assert "CH_COUNTS" in body, "счётчики чипов снова считаются по загруженному"


def test_active_chip_stays_visible_when_its_slice_is_empty():
    """Иначе чип, которым срез снять, исчезает, и пусто читается как «нет каналов»."""
    body = _fn("renderChFilterChips")
    assert "k===CH_FILTER" in body
