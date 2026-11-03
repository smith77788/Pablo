"""Список операций: тип, период и действия над несколькими операциями сразу.

Найдено живым обходом окна операций (03.11.2026): у списка был только фильтр
по статусу и поиск. «Что было с инвайтами за неделю» искалось перелистыванием,
типов у продукта больше восьмидесяти, а отменить или повторить десять
операций можно было только по одной.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function, miniapp_source

NODE = shutil.which("node")


class _Pool:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *a):
        self.sql.append((sql, a))
        if "GROUP BY op_type" in sql:
            return [{"op_type": "mass_invite", "n": 12}, {"op_type": "mass_publish", "n": 3}]
        return []

    async def fetchval(self, sql, *a):
        self.sql.append((sql, a))
        return 0

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


def _get(monkeypatch, query):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    pool = _Pool()

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/miniapp/operations" + query)
            return r.status, await r.json()
        finally:
            await client.close()

    status, data = asyncio.run(_go())
    return status, data, pool


def test_period_filter_reaches_the_query(monkeypatch):
    status, d, pool = _get(monkeypatch, "?days=7")
    assert status == 200 and d["days"] == 7
    page = [(s, a) for s, a in pool.sql if "FROM operation_queue oq" in s and "LIMIT" in s]
    assert page and "make_interval(days =>" in page[0][0] and 7 in page[0][1]


def test_garbage_period_is_refused_not_ignored(monkeypatch):
    status, d, _ = _get(monkeypatch, "?days=5")
    assert status == 400


def test_owner_types_come_with_counts(monkeypatch):
    status, d, _ = _get(monkeypatch, "")
    assert d["types"] == [{"op_type": "mass_invite", "n": 12}, {"op_type": "mass_publish", "n": 3}]


def test_screen_has_type_period_and_selection():
    src = miniapp_source()
    for marker in ('id="opsType"', 'id="opsDays"', 'onclick="toggleOpsSelect()"'):
        assert marker in src
    q = js_function("_opsQuery")
    assert "op_type" in q and "days" in q


@pytest.mark.skipif(not NODE, reason="нужен node")
@pytest.mark.parametrize("answer,expected", [(False, []), (True, ["1", "3"])])
def test_bulk_cancel_asks_once_and_respects_no(answer, expected):
    fn = js_function("cancelSelectedOps")
    prog = f"""
const sent = []; let asked = 0;
let OPS_ROWS = [{{id:1,status:'running'}},{{id:2,status:'done'}},{{id:3,status:'pending'}}];
const OPS_SEL = new Set([1,2,3]);
async function askConfirm() {{ asked++; return {json.dumps(answer)}; }}
async function api(u, o) {{ sent.push(u.split('/')[4]); return {{}}; }}
function plural(n,a,b,c) {{ return c; }} function toast() {{}} function loadOps() {{}}
function _opProgressMarkCancelled() {{}}
{fn}
(async () => {{ await cancelSelectedOps(); console.log(JSON.stringify({{sent, asked}})); }})();
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["asked"] == 1, "вопрос задан не один раз на всю пачку"
    assert res["sent"] == expected, res
