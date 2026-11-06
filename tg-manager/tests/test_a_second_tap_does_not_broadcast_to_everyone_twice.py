"""Второе нажатие «Отправить» не должно написать всей платформе второй раз.

Рассылка всем пользователям шла ВНУТРИ HTTP-запроса (0.05 с на человека) и
оставляла след в журнале только в КОНЦЕ. Значит, пока она идёт, первого
нажатия в базе нет вовсе — и второе нажатие от первого не отличить ничем.
А второе нажатие здесь штатное: ответа нет, кнопка живая, клиент повторяет
запрос по таймауту шлюза, Телеграм повторяет callback, если бот не ответил.
Каждый живой человек получал сообщение дважды, и отменить это нечем.

Теперь у нажатия есть личность (отпечаток текста), след пишется ДО отправки,
а повтор в окне возвращает итог первой рассылки и не шлёт ничего.
"""
from __future__ import annotations

import asyncio
import json

import pytest


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self.pool

    async def __aexit__(self, *a):
        return False


class _Pool:
    """Пул, который честно отвечает на поиск двойника по ЛИЧНОСТИ.

    Заглушка, отдающая двойника на любой запрос, зеленеет и на правильном, и
    на неправильном коде: «другой текст — другая рассылка» тогда не проверено.
    Поэтому двойники лежат по ident, и поиск сверяет именно его.
    """

    def __init__(self, users=(), twins=None):
        self.users = [{"user_id": u} for u in users]
        self.twins = dict(twins or {})
        self.inserts: list[dict] = []
        self.byid: dict[int, dict] = {}
        self.updates: list[tuple] = []
        self.queries: list[str] = []
        self.locks: list[str] = []
        self._next_id = 500

    def acquire(self):
        return _Acq(self)

    def transaction(self):
        return _Tx()

    async def fetch(self, q, *a):
        self.queries.append(" ".join(q.split()))
        if "FROM platform_users" in q:
            return self.users
        return []

    async def execute(self, q, *a):
        self.queries.append(" ".join(q.split()))
        if "pg_advisory_xact_lock" in q:
            self.locks.append(a[0])
        elif "UPDATE admin_audit_log" in q:
            self.updates.append(a)
            # Дополнение details — как в базе: патч ложится поверх записи, и
            # поиск двойника дальше видит ИТОГ, а не нули заявки.
            row = self.byid.get(a[0])
            if row is not None:
                row.update(json.loads(a[1]))
        return "OK"

    async def fetchval(self, q, *a):
        self.queries.append(" ".join(q.split()))
        if "INSERT INTO admin_audit_log" in q:
            details = json.loads(a[3])
            self.inserts.append(details)
            self.twins[details["ident"]] = details
            self._next_id += 1
            self.byid[self._next_id] = details
            return self._next_id
        if "SELECT details FROM admin_audit_log" in q:
            found = self.twins.get(a[2])
            return json.dumps(found) if found is not None else None
        return None

    async def fetchrow(self, q, *a):
        self.queries.append(" ".join(q.split()))
        return None


class _Bot:
    """Бот, который помнит, что уже лежало в журнале к моменту отправки."""

    sent: list[int] = []
    trace_at_first_send = None
    pool = None

    def __init__(self, *a, **kw):
        _Bot.sent = []
        _Bot.trace_at_first_send = None
        self.session = self

    async def send_message(self, user_id, text, **kw):
        if _Bot.trace_at_first_send is None and _Bot.pool is not None:
            _Bot.trace_at_first_send = len(_Bot.pool.inserts)
        _Bot.sent.append(user_id)

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    monkeypatch.setattr("aiogram.Bot", _Bot)
    monkeypatch.setattr("config.BOT_TOKEN", "1:x", raising=False)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def _run(pool, uid, text):
    from services import mini_app_api as M

    _Bot.pool = pool
    return asyncio.run(M._admin_broadcast_core(pool, uid, text))


def test_the_same_text_twice_in_a_row_is_one_broadcast():
    pool = _Pool(users=(11, 22, 33))
    first = _run(pool, 777, "Платформа переезжает в субботу")
    assert first == {"ok": True, "sent": 3, "failed": 0}
    assert len(_Bot.sent) == 3

    # Список отправленного чистим сами: повтор не доходит даже до создания
    # бота, поэтому его собственный сброс в __init__ не сработает — и
    # проверка «никому не писали» прошла бы на списке первой рассылки.
    _Bot.sent = []
    second = _run(pool, 777, "Платформа переезжает в субботу")
    assert second.get("duplicate") is True, second
    assert _Bot.sent == [], "второе нажатие снова написало людям"
    assert len(pool.inserts) == 1, "второе нажатие завело вторую заявку"


def test_the_trace_exists_before_the_first_message_goes_out():
    """След в конце бесполезен: пока рассылка идёт, повтор от неё не отличить."""
    pool = _Pool(users=(11, 22))
    _run(pool, 777, "текст")
    assert _Bot.trace_at_first_send == 1, (
        "к моменту первой отправки заявки в журнале не было — значит, второе "
        "нажатие в это окно снова разошлёт всем")


def test_another_text_is_another_broadcast():
    """Запрет повтора не должен запрещать вторую, другую рассылку."""
    pool = _Pool(users=(11, 22))
    _run(pool, 777, "первое сообщение")
    second = _run(pool, 777, "совсем другое сообщение")
    assert second == {"ok": True, "sent": 2, "failed": 0}
    assert len(_Bot.sent) == 2
    assert len(pool.inserts) == 2


def test_another_admin_is_not_blocked_by_the_first_ones_text():
    """Личность — админ И текст: окно одного админа не затыкает другого."""
    pool = _Pool(users=(11,))
    _run(pool, 777, "одинаковый текст")
    # Двойник ищется по admin_id тоже, а заглушка кладёт двойников по ident —
    # поэтому проверяем сам ключ блокировки: он включает админа.
    assert any("|777|" in k for k in pool.locks), pool.locks
    assert all("adminact|" in k for k in pool.locks), pool.locks


def test_the_identity_is_the_text_not_the_clock():
    """Отпечаток считается от текста: у двух нажатий подряд он один и тот же.

    Проверяем поведением, а не чтением исходника окном: окно промахивается от
    любой вставки рядом, а «искомого нет» тогда становится правдой и защита
    выключается молча (сторож: tests/test_no_silently_disabled_guards.py).
    Если личность снова посчитают от времени или от id, два РАЗНЫХ пула с тем
    же текстом дадут разные отпечатки — и дедуп не сработает ни разу.
    """
    first = _Pool(users=(11,))
    _run(first, 777, "один и тот же текст")
    second = _Pool(users=(11,))
    _run(second, 777, "один и тот же текст")
    assert first.inserts[0]["ident"] == second.inserts[0]["ident"], (
        "личность нажатия зависит не только от текста — от времени или от id "
        "она у двух нажатий разная, и запрет повтора не срабатывает")

    other = _Pool(users=(11,))
    _run(other, 777, "другой текст")
    assert other.inserts[0]["ident"] != first.inserts[0]["ident"], (
        "у разных текстов один отпечаток — вторая, законная рассылка будет "
        "заблокирована первой")


def test_a_duplicate_reports_the_first_run_numbers():
    """Админу нельзя отвечать «отправлено 0»: он решит, что не сработало."""
    pool = _Pool(users=(11, 22, 33))
    _run(pool, 777, "одно и то же")
    second = _run(pool, 777, "одно и то же")
    assert second["sent"] == 3, second
    assert second["recipients"] == 3, second
