"""Шапка и кнопка «Показать ещё» описывают ОТФИЛЬТРОВАННЫЙ список, а не весь.

Экран контактов (CRM) ходит за страницей по 50 и рядом получает `total`. Срез
по поиску и по метке считал только список, а `total` оставался числом ВСЕХ
контактов владельца. Видно это было дважды:

  * шапка писала «240 контактов» над тремя найденными. Число рядом с пустым
    поиском читается как «нашлось», и владелец делает вывод, что поиск
    сломан, а не что контактов три;
  * кнопку «Показать ещё» фронт показывает, пока `CRM_OFFSET < CRM_TOTAL`
    (`mini_app/index.html`). При трёх найденных и `total` в 240 кнопка стояла
    всегда: следующая страница отфильтрованной выборки пуста, список не растёт,
    кнопка не исчезает. Нажимать её можно было до бесконечности.

Почему проверка поведением, а не текстом исходника: условие собирается из
параметров запроса, и сравнивать литералы бессмысленно — у соседних ручек то же
условие доклеивается кусками (`operation_log`, `broadcasts_list` честны именно
так). Здесь запросы ловит поддельный пул, и правило смотрит, что у страницы и у
счётчика УСЛОВИЕ ОДНО.
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

UID = 640911
ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Pool:
    """Пул, запоминающий каждый запрос и его параметры."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return 240

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info: dict[str, str] = {}


def _handler(pool, path: str, method: str = "GET"):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == method and (info.get("path")
                                       or info.get("formatter")) == path:
            return route.handler
    raise AssertionError(f"роут {method} {path} не зарегистрирован")


def _call(pool, **query):
    return asyncio.run(_handler(pool, "/api/miniapp/crm/contacts")(_Req(**query)))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def _where(q: str) -> set[str]:
    """Условия WHERE одним множеством, без переносов и псевдонимов."""
    m = re.search(r"(?i)\bwhere\b(.*?)(?:\border\s+by\b|\blimit\b|$)", q,
                  re.DOTALL)
    assert m, f"в запросе нет WHERE: {' '.join(q.split())[:90]}"
    return {" ".join(p.split()).lower()
            for p in re.split(r"(?i)\s+AND\s+(?![^(]*\))", m.group(1))
            if p.strip()}


def _page_and_count(pool) -> tuple[tuple[str, tuple], tuple[str, tuple]]:
    page = count = None
    for q, a in pool.calls:
        if "LIMIT" in q and "COUNT(" not in q.upper():
            page = page or (q, a)
        elif "COUNT(*)" in q.upper() and "crm_contacts" in q:
            count = count or (q, a)
    assert page, "запроса страницы не было — проверять нечего"
    assert count, "запроса счётчика не было — проверять нечего"
    return page, count


# ── Поведение ручки ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,needle", [
    ({"q": "Иван"}, "ilike"),
    ({"tag": "vip"}, "any(tags)"),
])
def test_the_count_carries_the_same_filter_as_the_page(query, needle):
    pool = _Pool()
    _call(pool, **query)
    (pq, pa), (cq, ca) = _page_and_count(pool)
    assert needle in " ".join(pq.split()).lower(), (
        f"срез не дошёл до запроса страницы: {' '.join(pq.split())[:120]}")
    assert _where(pq) == _where(cq), (
        "счётчик считает не то, что показывает список: шапка соврёт о числе, "
        "а «Показать ещё» не исчезнет никогда.\n  страница: "
        f"{sorted(_where(pq))}\n  счётчик : {sorted(_where(cq))}")
    assert set(ca) <= set(pa), (
        f"параметры счётчика не из набора страницы: {ca} против {pa}")


def test_without_a_filter_both_count_everything():
    pool = _Pool()
    _call(pool)
    (pq, _), (cq, _) = _page_and_count(pool)
    assert _where(pq) == _where(cq) == {"owner_id=$1"}, (
        f"без среза условия разные: {sorted(_where(pq))} / {sorted(_where(cq))}")


def test_the_filter_value_is_a_parameter_not_glued_sql():
    """Склеенный в SQL срез — это и инъекция, и неверный счёт разом."""
    pool = _Pool()
    _call(pool, q="Иван'; DROP")
    (pq, pa), (cq, ca) = _page_and_count(pool)
    assert "DROP" not in pq and "DROP" not in cq, "строка поиска вклеена в SQL"
    assert any("Иван" in str(x) for x in pa), "поиск не передан параметром"
    assert any("Иван" in str(x) for x in ca), "счётчик не получил срез параметром"


# ── Почему это видно владельцу ──────────────────────────────────────────────

def test_the_button_is_hidden_by_that_very_total():
    """Правило выше защищает КНОПКУ. Разойдись фронт — защищать нечего.

    Проверка привязана к причине: кнопку «Показать ещё» фронт показывает,
    пока `CRM_OFFSET < CRM_TOTAL`, а `CRM_TOTAL` берётся из `total` ответа.
    Переедет это условие — правило выше останется зелёным, а кнопка снова
    сможет врать, поэтому связка сверяется здесь.
    """
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"CRM_TOTAL\s*=\s*d\.total", html), (
        "шапка контактов больше не берёт число из `total` ответа")
    assert re.search(r"crm-more'?\)?\.style\.display\s*=\s*CRM_OFFSET\s*<\s*CRM_TOTAL",
                     html), (
        "кнопка «Показать ещё» больше не прячется по CRM_OFFSET < CRM_TOTAL — "
        "проверьте, что она вообще прячется")
