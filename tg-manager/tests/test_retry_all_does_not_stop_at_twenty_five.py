"""«Перезапустить все недоведённые» не должно значить «двадцать пять навсегда».

Кнопка называет своё действие «все недоведённые операции за неделю», а ставит
за нажатие не больше двадцати пяти — и это правильный предохранитель: одно
нажатие иначе заваливает очередь. Неправдой была не цифра, а ДВЕ вещи вокруг
неё.

Первая: об остатке не говорили. Владелец читал «Перезапущено 25» как «всё», и
недоведённые операции так и оставались недоведёнными.

Вторая, из-за которой первую нельзя было вылечить одной подписью: выборка
брала двадцать пять НОВЕЙШИХ кандидатов, а уже перезапущенные из неё не
исключались. Второе нажатие брало тех же самых, дедуп по `retry_of_op`
превращал их в ничто, и до остальных дело не доходило никогда. То есть
«нажмите ещё раз» было бы честной подписью под неработающим действием —
поэтому сначала выборка, потом подпись.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re

import pytest
from aiohttp import web

from services import mini_app_api as M

UID = 907731
ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = "/api/miniapp/operations/retry_failed"


class _Pool:
    """Пул, у которого в недоведённых ровно `n` операций."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        if "operation_log ol" in q and "FROM operation_queue oq" in q:
            return [{"id": 1000 + i, "op_type": "bulk_dm_adhoc",
                     "label": f"Рассылка {i}", "status": "failed",
                     "done_items": 3, "total_items": 10, "err_cnt": 2}
                    for i in range(self.n)]
        if "id = ANY($1::bigint[])" in q:
            return [{"id": int(i), "op_type": "bulk_dm_adhoc", "params": "{}",
                     "label": f"Рассылка {i}", "total_items": 10} for i in a[0]]
        return []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        return None

    async def fetchval(self, q, *a):
        self.queries.append(q)
        return 0

    async def execute(self, q, *a):
        self.queries.append(q)
        return "OK"


class _Req:
    def __init__(self) -> None:
        self.rel_url = type("U", (), {"query": {}})()
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.match_info: dict[str, str] = {}

    async def json(self):
        return {}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    seq = iter(range(9000, 9500))

    async def _submit(pool, uid, op_type, params, **kw):
        return next(seq)

    monkeypatch.setattr(M._obus, "submit", _submit)
    M._cache.clear()
    yield
    M._cache.clear()


def _call(pool) -> dict:
    app = web.Application()
    M.setup_routes(app, pool)
    handler = None
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == "POST" and (info.get("path") or info.get("formatter")) == PATH:
            handler = route.handler
    assert handler, f"роут POST {PATH} не зарегистрирован"
    resp = asyncio.run(handler(_Req()))
    return json.loads(resp.body.decode("utf-8"))


def _scan_query(pool) -> str:
    for q in pool.queries:
        if "operation_log ol" in q and "FROM operation_queue oq" in q:
            return q
    raise AssertionError("запрос поиска недоведённых не найден")


def test_the_scan_skips_what_was_already_retried():
    """Иначе второе нажатие берёт тех же новейших, и остальные не берутся никогда."""
    pool = _Pool(40)
    _call(pool)
    q = _scan_query(pool)
    assert "retry_of_op" in q, (
        "выборка не исключает уже перезапущенные: нажатие за нажатием будет "
        "брать одних и тех же")
    assert re.search(r"NOT\s+EXISTS", q), q[-400:]


def test_the_remainder_is_named():
    pool = _Pool(40)
    d = _call(pool)
    assert d["retried"] == 25, "предохранитель на 25 за нажатие исчез"
    assert d["left"] == 15, d
    assert "Осталось недоведённых: 15" in d["note"], d["note"]


def test_nothing_is_promised_when_nothing_is_left():
    pool = _Pool(10)
    d = _call(pool)
    assert d["retried"] == 10 and d["left"] == 0
    assert "Осталось" not in d["note"], d["note"]


def test_an_empty_week_says_so():
    pool = _Pool(0)
    d = _call(pool)
    assert d["retried"] == 0 and "Недоведённых операций за неделю нет" in d["note"]


def test_the_screen_shows_the_note_the_server_wrote():
    """Подпись об остатке бесполезна, если экран её не показывает."""
    from tests.miniapp_source import js_function

    js = js_function("retryAllFailed")
    assert "d.note" in js, "экран не показывает подпись сервера"
