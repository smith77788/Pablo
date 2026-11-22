"""Сводка (`/api/miniapp/dashboard_realtime`): топ каналов за один проход журнала.

Активность канала считалась коррелированным подзапросом по `operation_audit`
на КАЖДЫЙ канал владельца: каналы × строки журнала. На стенде в 5000 каналов и
всего 2000 строк журнала один этот запрос шёл ~830 мс; на реальном журнале
(сотни тысяч строк) экран упирался в таймаут запроса. Теперь журнал
сворачивается по цели один раз и соединяется с каналами.

Проверяем на живой базе обе стороны: числа те же (совпадение по нику, по id и
случай, когда ник совпал с id текстом — строка журнала считается один раз), а
в плане запроса узел по `operation_audit` выполняется ровно один раз, а не по
разу на канал. Второе падает на старом запросе.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995311


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _audit_loops(plan: dict) -> list[int]:
    """Сколько раз выполнялся каждый узел плана над operation_audit."""
    out = []
    if plan.get("Relation Name") == "operation_audit":
        out.append(int(plan.get("Actual Loops", 0)))
    for sub in plan.get("Plans", []) or []:
        out.extend(_audit_loops(sub))
    return out


def test_top_channels_counted_right_and_journal_read_once(monkeypatch):
    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: OWNER)
    seen: list[str] = []
    orig_fetch = mini_app_api._safe_fetch

    async def _spy(pool, sql, *args, **kw):
        seen.append(sql)
        return await orig_fetch(pool, sql, *args, **kw)

    monkeypatch.setattr(mini_app_api, "_safe_fetch", _spy)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)

        async def _clean():
            await pool.execute("DELETE FROM operation_audit WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        try:
            await _clean()
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1, '+79990000311', 'sess', TRUE) RETURNING id", OWNER)
            # Канал «по нику», канал «по id», канал, у которого ник = id текстом,
            # и пустой канал.
            chans = [(-1001, "alpha"), (-1002, None), (-1003, "-1003"), (-1004, "quiet")]
            for cid, uname in chans:
                await pool.execute(
                    "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username) "
                    "VALUES($1, $2, $3, $4, $5)", OWNER, acc, cid, f"К{cid}", uname)
            targets = ["alpha"] * 3 + ["-1002"] * 2 + ["-1003"] * 4 + ["nobody", None]
            for t in targets:
                await pool.execute(
                    "INSERT INTO operation_audit(owner_id, action, result, target) "
                    "VALUES($1, 'post_publish', 'success', $2)", OWNER, t)

            app = web.Application()
            mini_app_api.setup_routes(app, pool)
            handler = next(r.handler for r in app.router.routes()
                           if getattr(r.resource, "canonical", "") ==
                           "/api/miniapp/dashboard_realtime")
            resp = await handler(make_mocked_request("GET", "/api/miniapp/dashboard_realtime"))
            data = json.loads(resp.body)

            top_sql = next(s for s in seen if "managed_channels" in s and "operation_audit" in s)
            plan = await pool.fetchval(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + top_sql, OWNER)
            plan = json.loads(plan) if isinstance(plan, str) else plan
            return data, _audit_loops(plan[0]["Plan"])
        finally:
            await _clean()
            await pool.close()

    data, loops = _run(_go())
    got = {c["name"]: c["subscribers"] for c in data["top_channels"]}
    assert got == {"К-1001": 3, "К-1002": 2, "К-1003": 4, "К-1004": 0}, got
    assert [c["name"] for c in data["top_channels"]][:3] == ["К-1003", "К-1001", "К-1002"]
    assert loops and max(loops) == 1, f"журнал читается по разу на канал: {loops}"
