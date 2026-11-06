"""Админ мини-аппа видит платежи платформы и отчёты об ошибках.

Живой обход (03.11.2026): в админке мини-аппа не было ни платежей, ни отчётов
об ошибках. «Застрявший» платёж (деньги ушли, подписка не включилась) искали
по логам, а отчёт, который пользователь отправил кнопкой «🐛 Сообщить об
ошибке» из мини-аппа, прочитать можно было только в боте.

Проверяется на настоящей базе, потому что запросы новые:
* срез по статусу и итог считаются одним условием;
* выручка — только по подтверждённым, за 30 дней и за всё время;
* смена статуса отчёта пишется и попадает в журнал действий админов;
* не-админ получает отказ, мусорный статус — 400, чужой номер — 404.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

ADMIN, U1, U2 = 7412345678, 1001, 1002

_SCHEMA = [
    "CREATE TABLE platform_users(user_id bigint PRIMARY KEY, username text, first_name text)",
    """CREATE TABLE payments(id serial PRIMARY KEY, user_id bigint NOT NULL, plan text NOT NULL,
           period_months int NOT NULL DEFAULT 1, currency text NOT NULL,
           amount_crypto numeric(18,6) NOT NULL, amount_usd numeric(10,2) NOT NULL,
           wallet_address text NOT NULL DEFAULT 'w', tx_hash text,
           status text NOT NULL DEFAULT 'pending', reference text NOT NULL UNIQUE,
           created_at timestamptz NOT NULL DEFAULT now(),
           confirmed_at timestamptz, tier text)""",
    """CREATE TABLE error_reports(id serial PRIMARY KEY, user_id bigint NOT NULL,
           description text NOT NULL, screenshot_id text, screenshot_path text,
           device_info jsonb, context jsonb, status varchar(20) NOT NULL DEFAULT 'new',
           assignee_id bigint, notes text, created_at timestamptz NOT NULL DEFAULT now(),
           updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE admin_audit_log(id bigserial PRIMARY KEY,
           admin_id bigint NOT NULL REFERENCES platform_users(user_id), action text NOT NULL,
           target_user_id bigint REFERENCES platform_users(user_id), details jsonb DEFAULT '{}',
           source text, created_at timestamptz DEFAULT now())""",
]


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _run(body, *, admin=True):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M

        name = "adm_" + uuid.uuid4().hex[:10]
        root = await asyncpg.connect(DSN)
        await root.execute(f'CREATE DATABASE "{name}"')
        pool = None
        orig = (M._get_uid, M._is_admin)
        M._get_uid = lambda r: ADMIN
        M._is_admin = lambda uid: admin
        try:
            pool = await asyncpg.create_pool(_db(name), min_size=1, max_size=2)
            for ddl in _SCHEMA:
                await pool.execute(ddl)
            await pool.executemany("INSERT INTO platform_users VALUES($1,$2,$3)",
                                   [(ADMIN, "boss", "Павел"), (U1, "anna", "Анна"), (U2, None, "Борис")])
            await pool.executemany(
                "INSERT INTO payments(user_id, plan, currency, amount_crypto, amount_usd, status,"
                " reference, created_at, confirmed_at, tier) VALUES($1,$2,$3,$4,$5,$6,$7,"
                " now() - make_interval(days => $8), CASE WHEN $6='confirmed'"
                " THEN now() - make_interval(days => $8) END, $9)",
                [(U1, "pro", "TON", 5, 20, "confirmed", "r1", 1, "pro"),
                 (U1, "pro", "TON", 5, 30, "confirmed", "r2", 90, "pro"),
                 (U2, "start", "USDT_TRC20", 10, 10, "pending", "r3", 0, None),
                 (U2, "start", "USDT_TRC20", 10, 10, "expired", "r4", 2, None)])
            await pool.executemany(
                "INSERT INTO error_reports(user_id, description, status, screenshot_id)"
                " VALUES($1,$2,$3,$4)",
                [(U1, "Не открывается рассылка", "new", "AgAD"),
                 (U2, "Кнопка не жмётся", "fixed", None)])
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool)
            finally:
                await client.close()
        finally:
            M._get_uid, M._is_admin = orig
            if pool is not None:
                await pool.close()
            await root.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await root.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def test_payments_slice_total_and_revenue():
    async def body(client, pool):
        a = await (await client.get("/api/miniapp/admin/payments")).json()
        c = await (await client.get("/api/miniapp/admin/payments?status=confirmed&limit=1")).json()
        bad = await client.get("/api/miniapp/admin/payments?status=hacked")
        return a, c, bad.status

    a, c, bad = _run(body)
    assert a["total"] == 4 and len(a["payments"]) == 4
    assert a["by_status"] == {"confirmed": 2, "pending": 1, "expired": 1}
    assert a["revenue"] == {"usd_30d": 20.0, "usd_all": 50.0, "payers": 1}, a["revenue"]
    assert c["total"] == 2 and len(c["payments"]) == 1 and c["has_more"] is True, (
        "итог среза посчитан не тем условием, что страница")
    p = c["payments"][0]
    assert p["status_ru"] == "оплачен" and p["username"] == "anna"
    assert p["tier_name"] and p["tier_name"] != "pro", "тариф ключом из базы, а не словом"
    assert bad == 400


def test_reports_list_and_status_change_are_audited():
    async def body(client, pool):
        new = await (await client.get("/api/miniapp/admin/reports?status=new")).json()
        rid = new["reports"][0]["id"]
        ok = await client.post(f"/api/miniapp/admin/report/{rid}/status",
                               json={"status": "fixing", "notes": "смотрю логи"})
        okj = await ok.json()
        junk = await client.post(f"/api/miniapp/admin/report/{rid}/status", json={"status": "boom"})
        missing = await client.post("/api/miniapp/admin/report/99999/status", json={"status": "fixed"})
        row = await pool.fetchrow("SELECT status, notes FROM error_reports WHERE id=$1", rid)
        audit = await pool.fetch("SELECT action, source FROM admin_audit_log")
        return new, okj, junk.status, missing.status, dict(row), [tuple(r) for r in audit]

    new, okj, junk, missing, row, audit = _run(body)
    assert new["total"] == 1 and new["by_status"] == {"new": 1, "fixed": 1}
    r = new["reports"][0]
    assert r["has_screenshot"] is True and r["status_ru"] == "новый" and r["username"] == "anna"
    assert okj["status_ru"] == "в работе"
    assert row == {"status": "fixing", "notes": "смотрю логи"}
    assert junk == 400 and missing == 404
    # Журнал пишет общий слой аудита — по записи на каждый изменяющий запрос
    # (и отказы тоже, с кодом ответа). Своей второй записи у ручки нет.
    assert audit[0] == (f"miniapp:POST /api/miniapp/admin/report/{new['reports'][0]['id']}/status",
                        "miniapp"), audit
    assert len(audit) == 3, f"смена статуса записана дважды или не записана: {audit}"


def test_the_journal_reads_miniapp_actions_in_russian():
    from services.mini_app_admin import _admin_action_ru
    assert _admin_action_ru("miniapp:POST /api/miniapp/admin/user/5/ban") == (
        "Мини-апп: заблокировал пользователя")
    assert _admin_action_ru("miniapp:POST /api/miniapp/admin/report/7/status") == (
        "Мини-апп: сменил статус отчёта об ошибке #7")


def test_not_an_admin_sees_nothing():
    async def body(client, pool):
        out = []
        for path in ("/api/miniapp/admin/payments", "/api/miniapp/admin/reports"):
            out.append((await client.get(path)).status)
        out.append((await client.post("/api/miniapp/admin/report/1/status",
                                      json={"status": "fixed"})).status)
        st = await pool.fetchval("SELECT status FROM error_reports WHERE id=1")
        return out, st

    codes, st = _run(body, admin=False)
    assert codes == [403, 403, 403] and st == "new"


def test_admin_screen_has_doors():
    from tests.miniapp_source import miniapp_source
    src = miniapp_source()
    assert 'onclick="openAdminPayments()"' in src and 'onclick="openAdminReports()"' in src
    assert "/api/miniapp/admin/report/" in src and "/status'" in src


def test_recent_platform_broadcasts_name_what_happened():
    """Экран рассылки показывает прошлые попытки — включая оборванную.

    Журнал админских действий `details` не отдаёт (там и причины банов), поэтому
    у экрана своя ручка ровно с его полями. Оборванная рассылка (запись без
    `finished`) — то, что владельцу важнее всего: часть людей письмо не
    получила, и это надо видеть ДО того, как нажать ещё раз.
    """
    async def body(client, pool):
        await pool.executemany(
            "INSERT INTO admin_audit_log(admin_id, action, details, source, created_at)"
            " VALUES($1,$2,$3::jsonb,$4, now() - make_interval(mins => $5))",
            [(ADMIN, "admin_broadcast",
              '{"recipients": 10, "sent": 10, "failed": 0, "finished": true,'
              ' "text": "Платформа переезжает", "ident": "aa"}', "miniapp", 1),
             (ADMIN, "admin_broadcast",
              '{"recipients": 10, "sent": 3, "failed": 1, "started": true,'
              ' "text": "Оборванная", "ident": "bb"}', "miniapp", 5),
             (ADMIN, "ban_user", '{"reason": "спам"}', "miniapp", 2)])
        ok = await (await client.get("/api/miniapp/admin/broadcast/recent")).json()
        return ok

    ok = _run(body)
    rows = ok["broadcasts"]
    assert len(rows) == 2, f"в выдачу попали не только рассылки: {rows}"
    assert rows[0]["text"] == "Платформа переезжает", rows[0]
    assert rows[0]["sent"] == 10 and rows[0]["finished"] is True, rows[0]
    assert rows[1]["finished"] is False, "оборванная рассылка названа законченной"
    assert rows[1]["sent"] == 3 and rows[1]["recipients"] == 10, rows[1]


def test_recent_platform_broadcasts_are_admin_only():
    async def body(client, pool):
        return (await client.get("/api/miniapp/admin/broadcast/recent")).status

    assert _run(body, admin=False) == 403
