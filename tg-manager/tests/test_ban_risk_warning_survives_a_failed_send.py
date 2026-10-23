"""Предупреждения монитора не теряются навсегда из-за одного сбоя отправки.

КЛАСС ДЕФЕКТА. Персистентная отметка анти-повтора (`db.notify_dedup_ok`)
занимается ДО отправки — иначе два события одного типа, пришедшие
одновременно, оба прошли бы проверку. Но у неудачной доставки должен быть путь
назад: `account_monitor` результат `notify_if_enabled` не читал, а окна здесь
суточные. Один временный сбой (Telegram ответил 5xx, сеть моргнула) сжигал
окно целиком, и предупреждение не приходило вообще до конца суток.

ЧЕМ ЭТО СТОИЛО ДОРОЖЕ ВСЕГО. Три места, и самое дорогое — «КРИТИЧЕСКИЙ РИСК
БАНА»: ровно те сутки, за которые аккаунт успевает получить бан, владелец
думает, что всё в порядке. Два других — «мало активных аккаунтов» (работать
стало нечем) и «сессия истекла» (аккаунт уже деактивирован автоматически).

ЧТО СЧИТАТЬ НЕУДАЧЕЙ. Не всякую: заблокированный бот и не нажатый /start
повторять бессмысленно (`db._NOTIFY_PERMANENT_MARKS`), отключённое владельцем
уведомление — не сбой. На них `notify_if_enabled` отвечает True, и отметка
обязана остаться, иначе каждый круг монитора жжёт лимиты Telegram впустую.

Тот же возврат слота уже сделан у итога операции
(`tests/test_operation_report_is_not_lost_to_a_failed_send.py`) и у алерта о
застрявших операциях — здесь он закрывает последние три места в мониторе.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db
from services import account_monitor


class _Bot:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls = 0

    async def send_message(self, uid, text, **kw):
        self.calls += 1
        if self.fail:
            raise self.fail


class _Pool:
    """Пул для анти-повтора: помнит занятые ключи и снятые отметки."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.taken: set[tuple[int, str]] = set()
        self.forgotten: list[tuple] = []

    async def fetch(self, q, *a):
        return [dict(r) for r in self.rows]

    async def fetchrow(self, q, *a):
        if "notification_dedup" in q:
            key = (int(a[0]), str(a[1]))
            if key in self.taken:
                return None
            self.taken.add(key)
            return {"user_id": a[0]}
        return None

    async def execute(self, q, *a):
        if "notification_dedup" in q and q.strip().upper().startswith("DELETE"):
            self.forgotten.append(tuple(a))
            self.taken.discard((int(a[0]), str(a[1])))
        return "DELETE 1"

    async def fetchval(self, q, *a):
        return 0


_TEMPORARY = RuntimeError("Telegram server error 502")
_PERMANENT = RuntimeError("Forbidden: bot was blocked by the user")


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Без пауз между попытками и с включёнными уведомлениями."""
    monkeypatch.setattr(db, "_NOTIFY_RETRY_DELAYS", ())

    async def _settings(pool, uid):
        return {}

    monkeypatch.setattr(db, "get_notification_settings", _settings)
    db._notify_cooldown.clear()
    yield
    db._notify_cooldown.clear()


# ── «Мало активных аккаунтов» ───────────────────────────────────────────────

def _low_pool():
    return _Pool([{"owner_id": 7, "active_count": 1}])


def test_low_accounts_alert_frees_the_window_after_a_lost_send():
    pool = _low_pool()
    bot = _Bot(_TEMPORARY)
    asyncio.run(account_monitor._check_and_alert(pool, bot))
    assert bot.calls >= 1, "подготовка: отправка не вызывалась"
    assert pool.forgotten, (
        "окно анти-повтора осталось занятым — владелец не узнает, что "
        "работать стало нечем, ещё сутки")

    bot2 = _Bot()
    asyncio.run(account_monitor._check_and_alert(pool, bot2))
    assert bot2.calls == 1, "повторная попытка так и не состоялась"


def test_low_accounts_alert_keeps_the_window_when_delivered():
    pool = _low_pool()
    bot = _Bot()
    asyncio.run(account_monitor._check_and_alert(pool, bot))
    assert bot.calls == 1 and not pool.forgotten

    bot2 = _Bot()
    asyncio.run(account_monitor._check_and_alert(pool, bot2))
    assert bot2.calls == 0, "одно и то же предупреждение ушло дважды за окно"


def test_low_accounts_alert_keeps_the_window_on_a_blocked_bot():
    """Повторять такое бессмысленно — только жжёт лимиты Telegram."""
    pool = _low_pool()
    asyncio.run(account_monitor._check_and_alert(pool, _Bot(_PERMANENT)))
    assert not pool.forgotten


# ── «Критический риск бана» — самое дорогое ─────────────────────────────────

@pytest.fixture
def _critical_risk(monkeypatch):
    from services import behavioral_engine

    async def _predict(pool, acc_id):
        return {"risk_level": "critical", "risk_score": 88,
                "reasons": ["всплеск flood за сутки"]}

    monkeypatch.setattr(behavioral_engine, "predict_ban_risk", _predict)


def _risk_pool():
    return _Pool([{"id": 3, "owner_id": 7, "username": "acc",
                   "first_name": None, "phone": None}])


def test_ban_risk_warning_frees_the_window_after_a_lost_send(_critical_risk):
    pool = _risk_pool()
    bot = _Bot(_TEMPORARY)
    asyncio.run(account_monitor._check_ban_risk(pool, bot))
    assert bot.calls >= 1, "подготовка: отправка не вызывалась"
    assert pool.forgotten, (
        "предупреждение о риске бана потеряно на сутки — ровно те сутки, за "
        "которые аккаунт успевает получить бан")
    assert pool.forgotten[0][1] == "ban_risk:3", pool.forgotten


def test_ban_risk_warning_is_retried_on_the_next_cycle(_critical_risk):
    pool = _risk_pool()
    asyncio.run(account_monitor._check_ban_risk(pool, _Bot(_TEMPORARY)))
    bot2 = _Bot()
    asyncio.run(account_monitor._check_ban_risk(pool, bot2))
    assert bot2.calls == 1, "следующий круг монитора так и не попробовал снова"


def test_ban_risk_warning_is_not_repeated_after_delivery(_critical_risk):
    pool = _risk_pool()
    bot = _Bot()
    asyncio.run(account_monitor._check_ban_risk(pool, bot))
    assert bot.calls == 1 and not pool.forgotten
    bot2 = _Bot()
    asyncio.run(account_monitor._check_ban_risk(pool, bot2))
    assert bot2.calls == 0, "владельца завалило повтором того же алерта"


def test_ban_risk_warning_keeps_the_window_on_a_blocked_bot(_critical_risk):
    pool = _risk_pool()
    asyncio.run(account_monitor._check_ban_risk(pool, _Bot(_PERMANENT)))
    assert not pool.forgotten


def test_a_healthy_account_gets_no_warning_at_all(monkeypatch):
    """Самопроверка пробника: без критического риска алерта быть не должно."""
    from services import behavioral_engine

    async def _predict(pool, acc_id):
        return {"risk_level": "low", "risk_score": 10, "reasons": []}

    monkeypatch.setattr(behavioral_engine, "predict_ban_risk", _predict)
    pool = _risk_pool()
    bot = _Bot()
    asyncio.run(account_monitor._check_ban_risk(pool, bot))
    assert bot.calls == 0 and not pool.forgotten


# ── «Сессия истекла» ────────────────────────────────────────────────────────

@pytest.fixture
def _dead_session(monkeypatch):
    """Телеграм отвечает «сессия мертва», коннект и паузы — заглушены."""
    from services import account_manager

    async def _check(session_str, acc=None, check_spambot=True):
        return {"status": "session_expired", "auth_error": True,
                "reason": "AuthKeyUnregistered", "display_name": ""}

    monkeypatch.setattr(account_manager, "check_account_status_full", _check)

    async def _sleep(_s):
        return None

    async def _wait_for(coro, timeout=None):
        return await coro

    monkeypatch.setattr(account_monitor.asyncio, "sleep", _sleep)
    monkeypatch.setattr(account_monitor.asyncio, "wait_for", _wait_for)


def _session_pool():
    return _Pool([{"id": 5, "owner_id": 7, "session_str": "x", "username": "acc",
                   "first_name": None, "phone": None}])


def test_expired_session_alert_frees_the_window_after_a_lost_send(_dead_session):
    pool = _session_pool()
    bot = _Bot(_TEMPORARY)
    asyncio.run(account_monitor._check_dead_sessions(pool, bot))
    assert bot.calls >= 1, "подготовка: отправка не вызывалась"
    assert pool.forgotten, (
        "аккаунт уже деактивирован автоматически, а владелец узнает об этом "
        "только через сутки")
    assert pool.forgotten[0][1] == "sess_expired:5", pool.forgotten


def test_expired_session_alert_keeps_the_window_when_delivered(_dead_session):
    pool = _session_pool()
    bot = _Bot()
    asyncio.run(account_monitor._check_dead_sessions(pool, bot))
    assert bot.calls == 1 and not pool.forgotten


def test_expired_session_alert_keeps_the_window_on_a_blocked_bot(_dead_session):
    pool = _session_pool()
    asyncio.run(account_monitor._check_dead_sessions(pool, _Bot(_PERMANENT)))
    assert not pool.forgotten
