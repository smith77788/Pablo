"""Админ-панель бота и мини-аппа оставляет след в admin_audit_log.

До 2026-11-02 журнал получал только рассылку из мини-аппа. Кнопки и команды
админ-панели бота (выдача тарифов, баны, ключи AI, бэкап) и изменяющие ручки
/api/miniapp/admin/* проходили бесследно. Внешние ключи журнала на
platform_users к тому же молча роняли запись для админа из ADMIN_IDS, который
ни разу не нажимал /start.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from aiogram.types import CallbackQuery, Chat, Message, User

from bot.middlewares import admin_audit
from services.mini_app_api import is_audited_admin_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _user(uid=7):
    return User(id=uid, is_bot=False, first_name="A")


def _msg(text):
    return Message(message_id=1, date=0, chat=Chat(id=7, type="private"),
                   from_user=_user(), text=text)


def _cb(data):
    return CallbackQuery(id="1", from_user=_user(), chat_instance="x", data=data)


def test_callbacks_and_commands_are_described():
    assert admin_audit.describe_admin_event(_cb("adm:grant:5")) == (7, "bot:cb:adm:grant:5")
    assert admin_audit.describe_admin_event(_msg("/backup")) == (7, "bot:cmd:/backup")


def test_free_text_is_never_journaled():
    """В FSM админ вводит ключи AI, токены и секретную фразу."""
    assert admin_audit.describe_admin_event(_msg("sk-or-v1-secret-key")) is None
    assert admin_audit.describe_admin_event(_msg("correct horse battery")) is None


class _Pool:
    def __init__(self):
        self.rows = []

    async def execute(self, sql, *args):
        if "admin_audit_log" in sql:
            self.rows.append(args)
        return "INSERT 1"


def test_middleware_writes_for_admins_only(monkeypatch):
    import services.mini_app_api as api
    monkeypatch.setattr(api, "_is_admin", lambda uid: uid == 7)
    pool = _Pool()
    mw = admin_audit.AdminAuditMiddleware()
    called = []

    async def handler(event, data):
        called.append(event)
        return "ok"

    assert asyncio.run(mw(handler, _cb("adm:ban:9"), {"pool": pool})) == "ok"
    assert pool.rows and pool.rows[0][0] == 7 and pool.rows[0][1] == "bot:cb:adm:ban:9"
    assert pool.rows[0][4] == "bot"

    monkeypatch.setattr(api, "_is_admin", lambda uid: False)
    pool2 = _Pool()
    asyncio.run(mw(handler, _cb("adm:ban:9"), {"pool": pool2}))
    assert pool2.rows == [] and len(called) == 2


def test_only_mutating_admin_api_requests_are_journaled():
    assert is_audited_admin_request("POST", "/api/miniapp/admin/user/5/ban")
    assert is_audited_admin_request("delete", "/api/miniapp/admin/x")
    assert not is_audited_admin_request("GET", "/api/miniapp/admin/users")
    assert not is_audited_admin_request("POST", "/api/miniapp/accounts")


def test_wiring_and_migration():
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "_admin_audit.install(admin_users_handler.router, admin_handler.router)" in main
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "app.middlewares.append(admin_audit_middleware)" in api
    adm = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert '"admin_session_granted"' in adm and 'Command("admin_log")' in adm
    sql = open(os.path.join(ROOT, "schema_v277_admin_audit_no_fk.sql"), encoding="utf-8").read()
    assert "DROP CONSTRAINT IF EXISTS admin_audit_log_admin_id_fkey" in sql
    assert "ADD COLUMN IF NOT EXISTS source" in sql
