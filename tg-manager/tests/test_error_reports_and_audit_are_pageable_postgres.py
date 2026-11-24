"""Отчёты об ошибках и журнал действий листаются — на живой базе.

РАЗРЫВ. Оба списка стояли на жёстком потолке и молчали о нём:

* `/api/miniapp/error_reports` — `LIMIT 20`. Отчёт — единственный канал связи
  владельца с разбором, и ответ приходит ОБРАТНО в тот же отчёт (`notes`).
  Двадцать первый отчёт вместе с ответом в нём не доходил никогда;
* `/api/miniapp/audit` — `LIMIT 200` за семь дней. Двести строк набирает одна
  массовая операция, а приходят сюда именно после большой: журнал обрывался на
  самом интересном.

Живая база, а не заглушка: у обоих появились `LIMIT $2 OFFSET $3` и запрос-
счётчик, причём у журнала счёт идёт по тому же условию с интервалом и джойном
к очереди. Заглушка пула типы параметров не проверяет — ошибка связывания или
разъехавшееся условие были бы невидимы.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995811
STRANGER = 995812
OP_ID = 99581100


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    await pool.execute("DELETE FROM error_reports WHERE user_id = ANY($1::bigint[])",
                       [OWNER, STRANGER])
    await pool.execute("DELETE FROM operation_log WHERE op_id = $1", OP_ID)
    await pool.execute("DELETE FROM operation_queue WHERE id = $1", OP_ID)


async def _seed(pool):
    await _clean(pool)
    for i in range(25):
        await pool.execute(
            "INSERT INTO error_reports(user_id, description, status) "
            "VALUES($1,$2,'new')", OWNER, f"Описание проблемы номер {i}")
    await pool.execute(
        "INSERT INTO error_reports(user_id, description, status) "
        "VALUES($1,'Чужой отчёт','new')", STRANGER)
    await pool.execute(
        "INSERT INTO operation_queue(id, owner_id, op_type, status, label) "
        "VALUES($1,$2,'mass_check','completed','Проверка флота')", OP_ID, OWNER)
    for i in range(12):
        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
            "VALUES($1,$2,$3,'ok','готово')", OP_ID, i, f"@target{i}")


def _ask(monkeypatch, route: str, query: str, uid: int = OWNER) -> dict:
    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: uid)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool)
            app = web.Application()
            mini_app_api.setup_routes(app, pool)
            handler = next(
                r.handler for r in app.router.routes()
                if getattr(r.resource, "canonical", "") == route)
            resp = await handler(make_mocked_request("GET", route + query))
            return json.loads(resp.body)
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


_REPORTS = "/api/miniapp/error_reports"
_AUDIT = "/api/miniapp/audit"


def test_reports_first_page_names_the_whole_count(monkeypatch):
    d = _ask(monkeypatch, _REPORTS, "")
    assert len(d["reports"]) == 20
    assert d["page"]["total"] == 25, "итог считается по всем отчётам владельца"
    assert d["page"]["has_more"] is True


def test_reports_next_page_does_not_repeat_or_skip(monkeypatch):
    first = [r["id"] for r in _ask(monkeypatch, _REPORTS, "?limit=20&offset=0")["reports"]]
    second = _ask(monkeypatch, _REPORTS, "?limit=20&offset=20")
    rest = [r["id"] for r in second["reports"]]
    assert len(rest) == 5
    assert not set(first) & set(rest), "страницы перекрылись"
    assert second["page"]["has_more"] is False
    assert len(set(first + rest)) == 25, "вместе страницы обязаны дать всё"


def test_another_owners_report_is_neither_shown_nor_counted(monkeypatch):
    d = _ask(monkeypatch, _REPORTS, "?limit=50")
    assert d["page"]["total"] == 25
    assert "Чужой отчёт" not in [r["description"] for r in d["reports"]]


def test_audit_counts_by_the_same_condition_it_lists_by(monkeypatch):
    """Итог и выдача обязаны считаться одним условием.

    Иначе экран печатает «показано 10 из 400», где 400 — другая выборка, и
    кнопка догрузки обещает страницы, которых нет.
    """
    d = _ask(monkeypatch, _AUDIT, "?limit=10&offset=0")
    assert len(d["entries"]) == 10
    assert d["page"]["total"] == 12, d["page"]
    assert d["page"]["has_more"] is True
    tail = _ask(monkeypatch, _AUDIT, "?limit=10&offset=10")
    assert len(tail["entries"]) == 2 and tail["page"]["has_more"] is False


def test_audit_never_shows_another_owners_operation(monkeypatch):
    d = _ask(monkeypatch, _AUDIT, "?limit=100", uid=STRANGER)
    assert d["entries"] == []
    assert d["page"]["total"] == 0
