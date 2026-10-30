"""Получатель кампании за пятисотым обязан открываться.

Журнал кампании в личные («кому не дошло») отдавался с потолком `_list_limit`
в 500 и БЕЗ смещения, а экран доклеивал страницы, удваивая `limit`: 100, 200,
400, 500 — и дальше стена. Кнопка «Показать ещё» при этом не исчезала, потому
что серверный итог был честным и больше показанного: она обещала записи,
достать которые было уже нечем. Тот же класс, что у сделок после пятидесятой и
у ссылок бота после тридцатой, но здесь цена выше: список открывают, чтобы
вычистить мёртвые контакты и переслать другим каналом, а кнопка
«📋 Скопировать номера» отдавала пятьсот из тысячи, не сказав об этом, — и
дальше по этому списку работали как по полному.

Поэтому проверка поведенческая: смещение доходит до записей за потолком, итог
считается по всему фильтру, порядок полный (`sent_at` не уникален), а экран
просит следующую страницу смещением и называет, сколько номеров скопирует.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M

UID = 640912
CAMPAIGN = 77
ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = "/api/miniapp/dm_campaign/{campaign_id}/log"


class _Pool:
    """Пул, отдающий страницу журнала и помнящий запросы."""

    def __init__(self, rows: int = 100, total: int = 1200) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows
        self.total = total

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return [{"tg_user_id": 1000 + i, "status": "blocked",
                 "error_msg": "USER_IS_BLOCKED", "sent_at": None,
                 "name": None, "username": None} for i in range(self.rows)]

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return {"name": "Осенняя серия"}

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return self.total

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info = {"campaign_id": str(CAMPAIGN)}


def _call(pool, **query) -> dict:
    app = web.Application()
    M.setup_routes(app, pool)
    handler = None
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == "GET" and (info.get("path") or info.get("formatter")) == PATH:
            handler = route.handler
    assert handler, f"роут GET {PATH} не зарегистрирован"
    resp = asyncio.run(handler(_Req(**query)))
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def _page_query(pool) -> str:
    for q, _ in pool.calls:
        if "dm_campaign_log" in q and "LIMIT" in q and "COUNT(*)" not in q:
            return q
    raise AssertionError("запрос страницы журнала не найден")


def _count_query(pool) -> str:
    for q, _ in pool.calls:
        if "COUNT(*)" in q and "dm_campaign_log" in q:
            return q
    raise AssertionError("запрос итога не найден")


def test_the_page_reaches_past_the_ceiling():
    """Запись за пятисотой достаётся смещением, а не ростом потолка."""
    pool = _Pool()
    d = _call(pool, limit="100", offset="900")
    q = _page_query(pool)
    assert "OFFSET 900" in q, f"смещение не доехало до запроса: {q[-200:]}"
    assert len(d["items"]) == 100
    assert d["page"]["offset"] == 900


def test_the_order_is_total():
    """`sent_at` не уникален: без разрыва ничьих страницы теряют записи."""
    pool = _Pool()
    _call(pool)
    q = _page_query(pool)
    order = q[q.index("ORDER BY"):q.index("LIMIT", q.index("ORDER BY"))]
    assert re.search(r"\bl\.id\b", order), f"порядок неполный: {order.strip()}"


def test_the_total_counts_the_whole_filter_not_the_page():
    pool = _Pool(rows=100, total=1200)
    d = _call(pool, limit="100")
    assert d["total"] == 1200, "итог посчитан не по всему фильтру"
    assert d["shown"] == 100, "длина страницы должна называть себя своим именем"
    assert "LIMIT" not in _count_query(pool), "итог считается по ограниченной выборке"


@pytest.mark.parametrize("offset,total,expect", [
    (0, 1200, True),      # впереди ещё 1100
    (1100, 1200, False),  # последняя страница: 1100 + 100 = 1200
    (0, 60, False),       # всё уместилось
])
def test_has_more_falls_exactly_on_the_last_page(offset, total, expect):
    pool = _Pool(rows=100, total=total)
    d = _call(pool, limit="100", offset=str(offset))
    assert d["page"]["has_more"] is expect


def _screen() -> str:
    from tests.miniapp_source import js_function

    return js_function("loadDmLogPage") + js_function("openDmLogMore")


def test_the_screen_asks_for_the_next_page_by_offset():
    js = _screen()
    assert "offset" in js, "экран не просит следующую страницу смещением"
    assert ".concat(" in js, "страница не доклеивается к уже показанному"
    assert "limit*2" not in js.replace(" ", ""), (
        "экран снова растит потолок: на пределе ручки это стена, а не страница")


def test_the_copy_button_says_how_many_it_will_copy():
    """«Скопировать номера (500)» при тысяче — та же правдоподобная неправда."""
    from tests.miniapp_source import js_function

    js = js_function("loadDmLogPage")
    assert "dmrCopyIds" in js, "кнопка копирования номеров пропала с экрана"
    btn = js[js.index("dmrCopyIds"):]
    assert "_DMRL_TOTAL" in btn and "из" in btn, (
        "кнопка копирования не говорит, что отдаёт лишь часть списка")


# --- Правило по всему фронту: страница не растёт потолком --------------------
#
# Тот же класс нашёлся не в двух местах, а в шестнадцати: «доклеить, подняв
# потолок» — это домашняя привычка, с которой писали все кнопки «Показать ещё».
# У каждой ручки свой предел (`_list_limit(request, 50, 1000)`), и на нём
# привычка превращается в стену: итог честный и больше показанного, поэтому
# кнопка остаётся, а нажатие уже ничего не добавляет.
#
# Переведены все: журнал кампании (500 при тысячах получателей — самый дорогой
# случай), «Конкуренты», «Слова», лента подписчиков, память ассистента, история
# проверок даты, нотариус, папки чатов, сообщения реле, запуски парсера,
# шаблоны активов, пакеты присутствия, недавние каналы, история клонирования,
# нарративные кампании, факты бота и задачи сетки. Каждому обработчику добавлен
# `_list_offset` и разрыв ничьих в ORDER BY: без полной сортировки страницы по
# смещению теряют и повторяют записи.
_GROWTH = (
    # X_LIMIT = more ? X_LIMIT + 200 : 50
    re.compile(r"(\w*[Ll][Ii][Mm][Ii][Tt]\w*)\s*=\s*[^;\n]*\b\1\b\s*\+\s*\d+"),
    # limit: Math.min(Q.limit*2, 500)
    re.compile(r"[Ll][Ii][Mm][Ii][Tt]\w*\s*\*\s*2"),
    # X_LIMIT += 200
    re.compile(r"(\w*[Ll][Ii][Mm][Ii][Tt]\w*)\s*\+=\s*\d+"),
)
# Долгов не осталось: все семнадцать экранов переведены на смещение. Список
# оставлен пустым намеренно — он часть храповика, а не исторической справки:
# появится новый растущий потолок, и `test_no_new_screen_pages_by_growing_its_limit`
# станет красным, а не «добавьте себя сюда».
_STILL_GROWING: set[str] = set()


def _grown(text: str) -> dict[str, str]:
    """{имя переменной или сам фрагмент: фрагмент} по каждому росту потолка."""
    out: dict[str, str] = {}
    for rule in _GROWTH:
        for m in rule.finditer(text):
            frag = m.group(0).strip()
            out[m.group(1) if m.groups() else frag] = frag
    return out


@pytest.mark.parametrize("src", [
    "COMP_LIMIT = more ? COMP_LIMIT + 200 : 50;",
    "openDmLog(Object.assign({}, Q, {limit: Math.min(Q.limit*2, 500)}));",
    "KW_LIMIT += 200;",
])
def test_the_rule_is_red_on_a_limit_that_grows(src):
    assert _grown(src), f"правило не видит растущего потолка: {src}"


def test_the_rule_is_not_red_on_offset_paging():
    assert not _grown(
        "CMPT_OFFSET = more ? CMPT_ROWS.length : 0;\n"
        "const d = await api('/api/miniapp/competitors?limit=50&offset='+CMPT_OFFSET);")


def _front_growth() -> dict[str, str]:
    from tests.miniapp_source import miniapp_source

    js = miniapp_source()
    assert js.count("moreBtn(") >= 18, (
        f"правило смотрит не туда: кнопок «Показать ещё» найдено "
        f"{js.count('moreBtn(')}")
    return _grown(js)


def test_no_new_screen_pages_by_growing_its_limit():
    extra = sorted(set(_front_growth()) - _STILL_GROWING)
    assert not extra, (
        "новый экран доклеивает страницы ростом потолка: на пределе ручки "
        "кнопка «Показать ещё» остаётся и перестаёт работать, а записи за "
        f"потолком недостижимы. Нужно смещение: {extra}")


def test_a_fixed_screen_leaves_the_list_of_debts():
    """Исправленный экран обязан исчезнуть из списка — иначе долг врёт."""
    left = sorted(_STILL_GROWING - set(_front_growth()))
    assert not left, (
        "эти экраны уже не растят потолок — вычеркните их из _STILL_GROWING, "
        f"иначе список долгов перестанет быть правдой: {left}")


def test_a_refresh_in_place_starts_the_list_over():
    """Перерисовка на месте не должна доклеивать страницу к показанному.

    Перевод на смещение добавил инвариант: массив строк и смещение — одно
    состояние. Удаление собеседника («забыть факты») перерисовывает экран
    вызовом с `keep=true`, и без сброса смещения страница по старому сдвигу
    доклеилась бы к уже показанным строкам, а забытый собеседник остался бы
    на экране.
    """
    from tests.miniapp_source import js_function

    js = js_function("forgetSemUser")
    assert "openSemFacts" in js, "перерисовка после удаления пропала"
    before = js[:js.index("openSemFacts(_SEMF_ARGS")]
    assert "SEMF_OFFSET = 0" in before and "SEMF_ROWS = []" in before, (
        "перед перерисовкой на месте не сброшены смещение и список")
