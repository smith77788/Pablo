"""Связанные записи прав и доступа идут одной транзакцией.

Три места писали по две-три таблицы независимыми запросами. Обрыв между ними
(передеплой Railway, разрыв соединения с базой) оставлял половину изменения:

* `create_workspace` — workspace без строки владельца в `workspace_members`.
  Доступ считается по членству, так что в такой workspace не может войти никто,
  включая его создателя, и удалить его из интерфейса тоже нельзя.
* `revoke_plan_from_user` — `platform_users` уже 'free', а `subscriptions` ещё
  активна. Права проверяет `get_plan` по `subscriptions`, а админка показывает
  `platform_users`: отзыв выглядит выполненным, но доступ остаётся.
* `grant_plan_to_user` — обратная половина: подписка активна, а `platform_users`
  ещё free, либо наоборот. Комментарий в коде прямо требовал, чтобы две даты не
  разошлись, но два независимых запроса этого не обеспечивают.

Заглушка пула не умеет откатывать по-настоящему — откатывает Postgres. Поэтому
тест проверяет то, от чего откат зависит: все связанные записи выполняются на
ОДНОМ соединении внутри ОТКРЫТОЙ транзакции, а не через pool.execute.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


def _run(coro):
    return asyncio.run(coro)


class _Tx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.depth += 1
        self._conn.events.append(("BEGIN", self._conn.depth))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.events.append(("ROLLBACK" if exc_type else "COMMIT", self._conn.depth))
        self._conn.depth -= 1
        return False


class _Conn:
    def __init__(self, owner):
        self.owner = owner
        self.depth = 0
        self.events: list[tuple] = []

    def transaction(self):
        return _Tx(self)

    async def _record(self, q, *a):
        self.owner.statements.append((" ".join(q.split()), self.depth))
        return None

    async def execute(self, q, *a):
        return await self._record(q, *a)

    async def fetchrow(self, q, *a):
        await self._record(q, *a)
        return self.owner.rows.pop(0) if self.owner.rows else None

    async def fetchval(self, q, *a):
        await self._record(q, *a)
        return None


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    """Пул-заглушка: помнит, на какой глубине транзакции выполнен каждый запрос."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.statements: list[tuple[str, int]] = []
        self.conn = _Conn(self)

    def acquire(self):
        return _Acquire(self.conn)

    # Вызовы мимо acquire() — это и есть регрессия: запрос вне транзакции.
    async def execute(self, q, *a):
        self.statements.append((" ".join(q.split()), 0))
        return None

    async def fetchrow(self, q, *a):
        self.statements.append((" ".join(q.split()), 0))
        return self.rows.pop(0) if self.rows else None

    async def fetchval(self, q, *a):
        self.statements.append((" ".join(q.split()), 0))
        return None


def _in_tx(pool, needle: str) -> bool:
    """Был ли запрос с этим фрагментом выполнен внутри транзакции."""
    hits = [d for q, d in pool.statements if needle in q]
    assert hits, f"запрос «{needle}» вообще не выполнялся: {[q for q, _ in pool.statements]}"
    return all(d >= 1 for d in hits)


def test_create_workspace_is_atomic():
    pool = _Pool(rows=[{"id": 7}])
    assert _run(db.create_workspace(pool, owner_id=1, name="ws")) == 7
    assert _in_tx(pool, "INSERT INTO workspaces")
    assert _in_tx(pool, "INSERT INTO workspace_members")


def test_revoke_plan_is_atomic():
    pool = _Pool()
    _run(db.revoke_plan_from_user(pool, user_id=1, admin_id=2))
    assert _in_tx(pool, "UPDATE platform_users SET current_plan='free'")
    assert _in_tx(pool, "UPDATE subscriptions SET is_active=false")


def test_grant_plan_is_atomic():
    pool = _Pool(rows=[{"plan": "pro", "expires_at": __import__("datetime").datetime(2027, 1, 1)}])
    _run(db.grant_plan_to_user(pool, user_id=1, admin_id=2, plan="pro", months=1))
    assert _in_tx(pool, "INSERT INTO subscriptions")
    assert _in_tx(pool, "UPDATE platform_users SET current_plan=$2")


def test_grant_free_plan_is_atomic():
    pool = _Pool()
    _run(db.grant_plan_to_user(pool, user_id=1, admin_id=2, plan="free", months=1))
    assert _in_tx(pool, "UPDATE subscriptions SET is_active=false")
    assert _in_tx(pool, "UPDATE platform_users SET current_plan='free'")


def test_payment_failure_does_not_undo_the_grant():
    """Сбой записи платежа откатывает только её (SAVEPOINT), доступ остаётся.

    Так было задумано в исходном коде: выдача доступа не должна пропадать из-за
    таблицы выручки. Перенос платежа в общую транзакцию отменил бы и выдачу.
    """

    class _FailingConn(_Conn):
        async def execute(self, q, *a):
            if "INSERT INTO payments" in q:
                self.owner.statements.append((" ".join(q.split()), self.depth))
                raise RuntimeError("payments недоступна")
            return await super().execute(q, *a)

    import datetime

    pool = _Pool(rows=[{"plan": "pro", "expires_at": datetime.datetime(2027, 1, 1)}])
    pool.conn = _FailingConn(pool)

    # Выдача не падает — платёж откатился отдельно.
    _run(db.grant_plan_to_user(pool, user_id=1, admin_id=2, plan="pro", months=1))
    assert _in_tx(pool, "INSERT INTO subscriptions")
    assert _in_tx(pool, "UPDATE platform_users SET current_plan=$2")
    # платёж выполнялся глубже выдачи — то есть под собственным SAVEPOINT
    pay_depth = [d for q, d in pool.statements if "INSERT INTO payments" in q][0]
    sub_depth = [d for q, d in pool.statements if "INSERT INTO subscriptions" in q][0]
    assert pay_depth > sub_depth, "платёж пишется не под собственным SAVEPOINT"


def test_audit_failure_does_not_break_revocation():
    """Журнал админ-действий не должен ронять уже состоявшийся отзыв прав."""

    class _NoAuditPool(_Pool):
        async def execute(self, q, *a):
            if "admin_audit_log" in q:
                raise RuntimeError("журнал недоступен")
            return await super().execute(q, *a)

    pool = _NoAuditPool()
    _run(db.revoke_plan_from_user(pool, user_id=1, admin_id=2))
    assert _in_tx(pool, "UPDATE subscriptions SET is_active=false")
