"""Доверенные («свои») сеансы аккаунта + завершение конкретного сеанса.

Просьба владельца (13.11.2026): «другой сеанс тоже мой, открытый на другом
устройстве — нужно пометить сеанс как своим». И: «в карточке аккаунта нет
возможности выбрать сеанс для завершения, а есть только закрыть все сеансы».

Дозор угона (services/session_watch) считает чужим КАЖДЫЙ сеанс, кроме нашего.
Помеченный «это я» (trusted_session_hashes, v371) не должен:
* попадать в счётчик чужих сеансов;
* поднимать тревогу;
* закрываться автозащитой.
"""
from __future__ import annotations

import asyncio
import os

from services import session_watch as sw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _foreign(*hashes):
    return [{"hash": h, "device": "iPhone 15", "app": "Telegram iOS",
             "country": "Netherlands", "ip": "203.0.113.5", "created": 1_700_000_000}
            for h in hashes]


class _Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


# ── Фильтр доверенных ────────────────────────────────────────────────────────

def test_drop_trusted_removes_only_my_sessions():
    foreign = _foreign(11, 22, 33)
    acc = {"id": 5, "trusted_session_hashes": [22]}
    kept = sw.drop_trusted(foreign, acc)
    assert {int(f["hash"]) for f in kept} == {11, 33}


def test_trusted_session_is_not_counted_or_alerted(monkeypatch):
    from database import db
    sent = []

    async def _ok(pool, owner, key, cooldown):
        return True

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        sent.append(text)
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)

    pool = _Pool()
    # Единственный чужой сеанс помечен своим — счётчик ноль, тревоги нет.
    acc = {"id": 5, "owner_id": 7, "username": "acc5",
           "foreign_session_hashes": None, "trusted_session_hashes": [11]}
    res = asyncio.run(sw.record(pool, acc, _foreign(11), bot=object()))
    assert res == {"foreign": 0, "new": 0, "written": True}
    assert not sent, "тревога пришла о сеансе, помеченном своим"
    sql, args = pool.executed[0]
    assert args[0] == 0, "доверенный сеанс попал в счётчик чужих"


def test_untrusted_session_still_counts(monkeypatch):
    from database import db
    sent = []

    async def _ok(pool, owner, key, cooldown):
        return True

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        sent.append(text)
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)

    pool = _Pool()
    acc = {"id": 5, "owner_id": 7, "username": "acc5",
           "foreign_session_hashes": None, "trusted_session_hashes": [22]}
    res = asyncio.run(sw.record(pool, acc, _foreign(11), bot=object()))
    assert res["foreign"] == 1 and res["new"] == 1
    assert sent, "чужой (не помеченный своим) сеанс не поднял тревогу"


# ── SQL-помощники записи ─────────────────────────────────────────────────────

def test_set_trust_on_drops_from_foreign_count():
    pool = _Pool()
    asyncio.run(sw.set_trust(pool, 7, 5, 11, True))
    sql, args = pool.executed[0]
    assert "trusted_session_hashes" in sql and "foreign_sessions" in sql
    assert args == (5, 7, 11)


def test_set_trust_off_only_removes_from_trusted():
    pool = _Pool()
    asyncio.run(sw.set_trust(pool, 7, 5, 11, False))
    sql, _ = pool.executed[0]
    assert "array_remove(trusted_session_hashes" in sql
    assert "foreign_sessions" not in sql, "снятие пометки не должно трогать счётчик"


def test_drop_one_decrements_the_counter():
    pool = _Pool()
    asyncio.run(sw.drop_one(pool, 7, 5, 11))
    sql, args = pool.executed[0]
    assert "GREATEST" in sql and "array_remove(foreign_session_hashes" in sql
    assert args == (5, 7, 11)


# ── Автозащита не трогает доверенный сеанс ───────────────────────────────────

def test_guard_never_closes_a_trusted_session():
    src = _src("services/session_watch.py")
    guard = src[src.index("async def guard("):]
    guard = guard[:guard.index("\nasync def ", 1)] if "\nasync def " in guard[10:] else guard
    assert "drop_trusted(profile.get(\"authorizations\")" in guard, (
        "автозащита должна снимать доверенные сеансы до расчёта новых")


# ── Бэкенд: функции и эндпоинты ──────────────────────────────────────────────

def test_engine_has_per_session_list_and_close():
    src = _src("services/profile_setter_engine.py")
    assert "async def list_sessions(" in src
    assert "async def close_one_session(" in src
    assert "ResetAuthorizationRequest" in src, "закрытие одного сеанса идёт по hash"


def test_api_exposes_three_session_routes():
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "GET /api/miniapp/account/{acc_id}/sessions" in snap
    assert "POST /api/miniapp/account/{acc_id}/session/close" in snap
    assert "POST /api/miniapp/account/{acc_id}/session/trust" in snap


# ── Фронт: экран сеансов в карточке ──────────────────────────────────────────

def test_card_opens_sessions_and_offers_mine_and_close():
    html = _src("mini_app/index.html")
    js = _src("mini_app/screens/accounts_list.js")
    assert 'id="s-accsessions"' in html, "нет экрана сеансов"
    assert "openAccSessions(" in html, "из карточки не открыть сеансы"
    assert "async function openAccSessions(" in js
    for fn in ("accSessTrust(", "accSessClose(", "accSessCloseAll("):
        assert fn in js, f"нет действия {fn}"
    assert "/session/trust" in js and "/session/close" in js
    # «Это я» и «Завершить» — оба действия на строке сеанса.
    assert "Это я" in js and "Завершить" in js
