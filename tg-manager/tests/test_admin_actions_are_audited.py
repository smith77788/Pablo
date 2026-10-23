"""Админские действия оставляют след, а сбой журнала не ломает действие.

Выдача подписки, отзыв, бан и разбан в admin_audit_log писались, а рассылка
ВСЕМ пользователям — нет. Это самое далеко идущее действие продукта: одно
нажатие пишет каждому живому пользователю, отменить это нечем, и восстановить
«кто и что разослал» было неоткуда.

Второе: запись в журнал была выписана в каждом месте отдельно, и места
разошлись — часть оборачивалась в try, часть нет. Где не оборачивалась, сбой
ЗАПИСИ (у admin_id внешний ключ на platform_users) ронял запрос уже ПОСЛЕ
применения действия: бан стоял, а админ видел 500 и не знал, сработало ли.
Теперь вход один — `database.db.log_admin_action`, и он fail-soft.
"""
from __future__ import annotations

import asyncio
import json

import pytest


class _Pool:
    """Заглушка пула: помнит запросы и умеет ломать запись в журнал."""

    def __init__(self, users=(), fail_audit=False):
        self.users = [{"user_id": u} for u in users]
        self.fail_audit = fail_audit
        self.audit: list[tuple] = []
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(" ".join(q.split()))
        if "FROM platform_users" in q:
            return self.users
        return []

    async def execute(self, q, *a):
        one = " ".join(q.split())
        self.queries.append(one)
        if "admin_audit_log" in q:
            if self.fail_audit:
                raise RuntimeError("нет такого admin_id в platform_users")
            self.audit.append(a)
        return "INSERT 1"

    async def fetchval(self, q, *a):
        self.queries.append(" ".join(q.split()))
        return None

    async def fetchrow(self, q, *a):
        self.queries.append(" ".join(q.split()))
        return None


class _Bot:
    """Заглушка бота: считает отправленное, на один адрес падает."""

    sent: list[int] = []

    def __init__(self, *a, **kw):
        _Bot.sent = []
        self.session = self

    async def send_message(self, user_id, text, **kw):
        if user_id == 666:
            raise RuntimeError("пользователь заблокировал бота")
        _Bot.sent.append(user_id)

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _fast_and_fake(monkeypatch):
    monkeypatch.setattr("aiogram.Bot", _Bot)
    monkeypatch.setattr("config.BOT_TOKEN", "1:x", raising=False)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def _audited(pool):
    """Записи журнала как (admin_id, action, target, details-dict)."""
    out = []
    for admin_id, action, target, details in pool.audit:
        out.append((admin_id, action, target, json.loads(details)))
    return out


def test_broadcast_leaves_a_trace_with_counts():
    from services import mini_app_api as M

    pool = _Pool(users=(11, 22, 666, 33))
    res = asyncio.run(M._admin_broadcast_core(pool, 777, "Привет всем"))

    assert res == {"ok": True, "sent": 3, "failed": 1}
    rec = _audited(pool)
    assert len(rec) == 1, f"рассылка без следа в журнале: {pool.queries}"
    admin_id, action, target, details = rec[0]
    assert admin_id == 777
    assert action == "admin_broadcast"
    assert target is None
    assert details["recipients"] == 4
    assert details["sent"] == 3
    assert details["failed"] == 1
    assert details["text"] == "Привет всем"


def test_broadcast_to_nobody_is_still_recorded():
    """Ноль получателей — тоже факт: нажатие было."""
    from services import mini_app_api as M

    pool = _Pool(users=())
    assert asyncio.run(M._admin_broadcast_core(pool, 777, "текст")) == {
        "ok": True, "sent": 0, "failed": 0}
    assert len(_audited(pool)) == 1


def test_broadcast_records_what_went_out_before_it_broke(monkeypatch):
    """Обрыв на середине не должен стирать факт, что людям уже написали.

    Обрыв приходит НЕ из отправки (её падения считаются как failed), а снаружи
    цикла — так ведёт себя отменённый запрос: шлюз закрыл соединение, aiohttp
    отменил задачу обработчика.
    """
    from services import mini_app_api as M

    pool = _Pool(users=(1, 2, 3))
    calls = {"n": 0}

    async def _sleep_then_abort(_s):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep_then_abort)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(M._admin_broadcast_core(pool, 777, "текст"))

    rec = _audited(pool)
    assert len(rec) == 1, "обрыв стёр запись о начатой рассылке"
    assert rec[0][3]["sent"] == 2, rec[0][3]
    assert rec[0][3]["recipients"] == 3


def test_audit_failure_does_not_break_the_action():
    """Журнал не смог записаться — действие всё равно состоялось."""
    from services import mini_app_api as M

    pool = _Pool(users=(11, 22), fail_audit=True)
    res = asyncio.run(M._admin_broadcast_core(pool, 777, "текст"))
    assert res == {"ok": True, "sent": 2, "failed": 0}


def test_log_admin_action_is_the_single_entry_point():
    """Прямых INSERT в журнал не остаётся: иначе места снова разойдутся."""
    from database import db

    src = open(db.__file__, encoding="utf-8").read()
    # Один — внутри самого log_admin_action.
    assert src.count("INSERT INTO admin_audit_log") == 1, (
        "в db.py снова появилась прямая запись в admin_audit_log — "
        "пишите через log_admin_action")


def test_ban_still_applies_when_the_journal_fails():
    """Раньше сбой журнала здесь ронял запрос ПОСЛЕ применения бана."""
    from database import db

    pool = _Pool(fail_audit=True)
    asyncio.run(db.ban_user(pool, 42, 777, "спам"))
    assert any("UPDATE platform_users" in q and "is_banned=true" in q
               for q in pool.queries), pool.queries
