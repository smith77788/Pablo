"""Админ мини-аппа видит операции платформы и журнал действий админов.

Живой обход (03.11.2026): в админке мини-аппа были только пользователи,
статистика и рассылка. Ручки «статистика операций платформы» и «лента целей»
были на сервере без единой кнопки, а первая из них падала с 500 при ЛЮБОМ
вызове: каждое поле строки приводилось к int, включая op_type. Журнала
действий админов (admin_audit_log) в мини-аппе не было вовсе — только командой
бота /admin_log. Плитки «Операции/день», «В очереди», «Выполняется» вели в
собственную очередь админа, а не в платформенную.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from tests.miniapp_source import js_function, miniapp_source

_NOW = datetime.now(timezone.utc)


class _Pool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        if "GROUP BY op_type" in sql:
            return [{"op_type": "mass_invite", "total": 10, "done": 7, "failed": 3,
                     "avg_duration_s": 125.4}]
        if "FROM admin_audit_log" in sql:
            return [{"id": 1, "admin_id": 1, "action": "bot:cmd:/backup", "target_user_id": None,
                     "source": "bot", "created_at": _NOW, "admin_username": "boss",
                     "target_username": None}]
        return []

    async def fetchval(self, *a, **k): return 1
    async def fetchrow(self, *a, **k): return None
    async def execute(self, *a, **k): return "OK"


def _get(monkeypatch, path, admin=True):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 1)
    monkeypatch.setattr(mini_app_api, "_is_admin", lambda uid: admin)

    pool = _Pool()

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get(path)
            return r.status, await r.json()
        finally:
            await client.close()

    status, body = asyncio.run(_go())
    return status, body, pool


def test_platform_ops_stats_no_longer_crash(monkeypatch):
    status, d, _pool = _get(monkeypatch, "/api/miniapp/admin/ops/stats")
    assert status == 200, d
    row = d["stats"][0]
    assert row["op_type"] == "mass_invite" and row["fail_pct"] == 30.0
    assert row["type_ru"] and row["type_ru"] != "mass_invite"


def test_admin_actions_read_in_russian(monkeypatch):
    status, d, _pool = _get(monkeypatch, "/api/miniapp/admin/actions")
    assert status == 200 and d["actions"][0]["action_ru"] == "Команда бота: /backup"


def test_admin_actions_are_admin_only(monkeypatch):
    status, _, _pool = _get(monkeypatch, "/api/miniapp/admin/actions", admin=False)
    assert status == 403


def test_admin_screen_has_doors():
    src = miniapp_source()
    assert 'onclick="openAdminOps()"' in src and 'onclick="openAdminActions()"' in src
    stats = js_function("openAdminStats")
    assert "'openOps()'" not in stats, "плитки платформы снова ведут в свою очередь"


def test_the_window_of_the_summary_is_chosen_and_the_list_is_checked(monkeypatch):
    """Окно сводки выбирается, и выбор проверяет сервер.

    Семь дней были вшиты в SQL, а у экрана не было ни одного действия — ровно
    жалоба владельца про экраны, где «всё не кликабельно». Теперь окно
    приходит в запросе; принимаем только 1, 7 и 30, и число уходит в запрос
    ПАРАМЕТРОМ, а не текстом в SQL.
    """
    status, d, pool = _get(monkeypatch, "/api/miniapp/admin/ops/stats?days=30")
    assert status == 200 and d["days"] == 30, d
    sql, args = next((c for c in pool.calls if "GROUP BY op_type" in c[0]), (None, None))
    assert sql and "make_interval" in sql, sql
    assert "30 days" not in sql and "INTERVAL '" not in sql, (
        "окно подставлено в SQL текстом — так в запрос попадёт что угодно")
    assert args == (30,), args

    status, d, _ = _get(monkeypatch, "/api/miniapp/admin/ops/stats?days=5")
    assert status == 400, (status, d)
    assert "окно" in d["error"].lower() and "30" in d["error"], d

    status, d, _ = _get(monkeypatch, "/api/miniapp/admin/ops/stats?days=7+OR+1%3D1")
    assert status == 400, (status, d)


def test_the_summary_screen_is_not_a_wall_of_numbers():
    """У сводки операций есть действия, а не только числа.

    Экран был чисто смотровой: обход (deploy/scripts/render_miniapp.mjs sweep)
    мерил на нём НОЛЬ действий — ни выбора окна, ни обновления, ни перехода от
    «много ошибок» к самим целям. Проверка статическая, поэтому работает в CI,
    где браузера нет.
    """
    body = js_function("openAdminOps")
    assert "admOpsWindow(" in body, "окно сводки снова нельзя переключить"
    assert "openAdminOps(true)" in body, "со экрана пропало обновление"
    assert "admOpsType(" in body, "строка типа операций снова не ведёт к целям"
    assert "seg-opt" in body, "переключатель окна рисуется не переключателем"
    feed = js_function("admOpsFeed")
    assert "Сбросить" in feed, "фильтр по типу нечем снять"
