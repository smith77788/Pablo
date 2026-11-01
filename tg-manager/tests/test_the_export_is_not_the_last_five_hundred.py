"""Выгрузка операций обрывалась на пятистах и не говорила об этом.

Кнопка «📥 Экспорт CSV» на экране операций отдаёт файл, который несут ИЗ
приложения наружу: в отчёт, в таблицу, в переписку с подрядчиком. Отдавались
последние пятьсот строк — у активного владельца это полторы сотни часов
работы, а не история. Обрезанная выгрузка хуже отказа: по ней принимают
решения, не зная, что её обрезали.

Тот же класс, что «📋 Скопировать номера (500)» в журнале кампании, и он
стоит отдельного правила: предохранитель от выгрузки на миллион строк нужен,
но он обязан быть предохранителем — высоким и названным, — а не продуктовым
ограничением, о котором владелец не знает.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from aiohttp import web

from services import mini_app_api as M

UID = 330188
PATH = "/api/miniapp/operations/export"


class _Pool:
    def __init__(self, rows: int, total: int) -> None:
        self.rows = rows
        self.total = total
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        return [{"id": i, "op_type": "mass_publish", "label": f"Публикация {i}",
                 "status": "done", "total_items": 10, "done_items": 10,
                 "err_cnt": 0, "created_at": None, "finished_at": None}
                for i in range(self.rows)]

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        return self.total

    async def execute(self, q, *a):
        self.queries.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = ""
        self.match_info: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def _call(pool, **query):
    app = web.Application()
    M.setup_routes(app, pool)
    handler = None
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == "GET" and (info.get("path") or info.get("formatter")) == PATH:
            handler = route.handler
    assert handler, f"роут GET {PATH} не зарегистрирован"
    return asyncio.run(handler(_Req(**query)))


def _export_query(pool) -> tuple[str, tuple]:
    for q, a in pool.queries:
        if "FROM operation_queue" in q and "COUNT(*)" not in q:
            return q, a
    raise AssertionError("запрос выгрузки не найден")


def test_the_cap_is_a_safety_valve_not_a_page():
    pool = _Pool(rows=120, total=120)
    _call(pool)
    q, args = _export_query(pool)
    assert 50_000 in args, f"потолок выгрузки не пятьдесят тысяч: {args}"
    assert "LIMIT 500\n" not in q and "LIMIT 500 " not in q


def test_the_order_is_total():
    """Выгрузка без полного порядка повторяет и теряет строки на равных датах."""
    pool = _Pool(rows=10, total=10)
    _call(pool)
    q, _ = _export_query(pool)
    order = q[q.index("ORDER BY"):]
    assert re.search(r"\bid\b", order.split("LIMIT")[0]), order[:120]


def test_the_json_format_says_how_much_there_is():
    pool = _Pool(rows=120, total=4200)
    resp = _call(pool, format="json")
    d = json.loads(resp.body.decode("utf-8"))
    assert d["total"] == 4200, d.keys()
    assert d["truncated"] is False


def test_the_json_format_admits_the_cap():
    pool = _Pool(rows=50_000, total=123_456)
    resp = _call(pool, format="json")
    d = json.loads(resp.body.decode("utf-8"))
    assert d["truncated"] is True and d["total"] == 123_456


def test_the_csv_still_comes_back_as_a_file():
    pool = _Pool(rows=3, total=3)
    resp = _call(pool)
    assert resp.content_type == "text/csv"
    assert "Публикация 0" in resp.text


def test_the_screen_warns_before_a_truncated_export():
    """Файл уносят наружу: про обрезку надо сказать ДО скачивания."""
    from tests.miniapp_source import js_function

    js = js_function("exportOpsCSV")
    assert "OPS_TOTAL" in js and "askConfirm" in js, (
        "экран скачивает молча: владелец не узнает, что история больше файла")
    assert "OPS_EXPORT_CAP" in js, "в предупреждении не назван потолок выгрузки"
    # Потолок — одно число в двух местах, поэтому связываем их: иначе сервер
    # поднимут, а предупреждение останется пугать старой цифрой.
    from tests.miniapp_source import api_source, miniapp_source

    front = re.search(r"OPS_EXPORT_CAP\s*=\s*(\d+)", miniapp_source())
    # Именно присваивание потолка отдельной строкой: `cap=500` бывает и
    # именованным аргументом чужого вызова (selection_ids(..., cap=500)), и
    # первое вхождение в файле оказалось бы чужим числом.
    back = re.search(r"^\s*cap\s*=\s*([\d_]+)\s*$", api_source(), re.M)
    assert front and back, "потолок выгрузки больше не находится в коде"
    assert int(front.group(1)) == int(back.group(1).replace("_", "")), (
        f"экран грозит {front.group(1)}, а сервер отдаёт {back.group(1)}")
