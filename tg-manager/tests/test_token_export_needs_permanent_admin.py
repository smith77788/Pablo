"""Выгрузка токенов всех ботов — только для постоянных админов (ADMIN_IDS).

Кнопка «Экспорт токенов» отдавала файл с токенами ботов ВСЕХ клиентов любому
админу, включая вошедшего по секретной фразе. Утечка фразы одним нажатием
отдавала все боты платформы, и следа в журнале не оставалось.
Тест падает без фикса.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.handlers import admin


class _Msg:
    def __init__(self):
        self.docs: list = []
        self.texts: list[str] = []

    async def answer_document(self, f, **k):
        self.docs.append(f)

    async def answer(self, text, **k):
        self.texts.append(text)


class _Pool:
    def __init__(self):
        self.logged: list = []

    async def fetch(self, *a):
        return []

    async def execute(self, q, *a):
        if "admin_audit_log" in q or "admin_actions" in q:
            self.logged.append(a)
        return "OK"


async def test_session_admin_cannot_export(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "1")
    msg = _Msg()
    cb = SimpleNamespace(from_user=SimpleNamespace(id=777), message=msg)
    await admin._adm_send_tokens_file(cb, _Pool())
    assert msg.docs == []
    assert msg.texts and "ADMIN_IDS" in msg.texts[0]


async def test_permanent_admin_export_is_logged(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "1")
    seen = []

    async def _log(pool, admin_id, action, target, details=None, **k):
        seen.append(action)

    monkeypatch.setattr(admin.db, "log_admin_action", _log)

    async def _fetch_bots(pool, q):
        return []

    import database.db as _d
    monkeypatch.setattr(_d, "fetch_bots", _fetch_bots)
    msg = _Msg()
    cb = SimpleNamespace(from_user=SimpleNamespace(id=1), message=msg)
    await admin._adm_send_tokens_file(cb, _Pool())
    assert msg.docs and seen == ["export_all_bot_tokens"]


async def test_session_admin_cannot_touch_env(monkeypatch):
    """Через переменные окружения вошедший по фразе дописывал себя в ADMIN_IDS."""
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setattr(admin, "_session_admins", {777: 9e18} if isinstance(
        admin._session_admins, dict) else {777})
    called = []

    async def _never(*a, **k):
        called.append(a)

    monkeypatch.setattr(admin, "_adm_env_list", _never)
    monkeypatch.setattr(admin, "_adm_env_edit_ask", _never)
    for data in ("adm:env_list", "adm:env_edit:ADMIN_IDS", "adm:env_add"):
        msg = _Msg()
        msg.edit_text = msg.answer

        async def _ans(*a, **k):
            return None

        cb = SimpleNamespace(from_user=SimpleNamespace(id=777), message=msg,
                             data=data, answer=_ans)
        await admin.cb_admin(cb, _Pool(), None, None)
        assert msg.texts and "ADMIN_IDS" in msg.texts[0], (data, msg.texts)
    assert called == []
