"""Код-приглашение в workspace обязан протухать.

Вход в workspace — это доступ к ботам и каналам его владельца: `db.get_bot`
пускает по членству и отдаёт бота вместе с РАСШИФРОВАННЫМ токеном. То есть
код приглашения — это ключ от чужой инфраструктуры.

Колонка `workspace_invites.expires_at` в схеме была с самого начала, но её
никто не заполнял (INSERT её не упоминал) и никто не проверял (`use_...`
смотрел только `uses_left>0`). Код на пять входов работал вечно: утёкший или
переданный полгода назад, он оставался рабочим ключом, и отозвать его было
нечем — в интерфейсе отзыва нет.

Теперь срок проставляется при выдаче и проверяется при входе. У строк,
созданных до этой правки, expires_at пуст — они считаются живущими те же дни
от создания, а не вечно.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from database import db


class _Conn:
    def __init__(self, log, invite):
        self._log, self._invite = log, invite

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchrow(self, sql, *a):
        self._log.append(sql)
        return self._invite

    async def fetchval(self, sql, *a):
        self._log.append(sql)
        return None

    async def execute(self, sql, *a):
        self._log.append(sql)
        return "INSERT 0 1"


class _Pool:
    def __init__(self, invite=None):
        self.log: list[str] = []
        self._invite = invite

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool.log, pool._invite)

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def execute(self, sql, *a):
        self.log.append(sql)
        return "INSERT 0 1"

    async def fetchrow(self, sql, *a):
        self.log.append(sql)
        return None

    async def fetchval(self, sql, *a):
        self.log.append(sql)
        return None


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_ttl_is_a_sane_number_of_days():
    assert 1 <= db.WORKSPACE_INVITE_TTL_DAYS <= 90, (
        "срок жизни ключа от чужой инфраструктуры должен быть коротким и конечным"
    )


def test_issued_invite_carries_an_expiry(monkeypatch):
    pool = _Pool()

    async def _role(*a, **kw):
        return "owner"

    monkeypatch.setattr(db, "get_workspace_role", _role)
    code = _run(db.create_workspace_invite(pool, 5, 777))

    assert code, "владелец обязан получать код"
    inserts = [q for q in pool.log if "INSERT INTO workspace_invites" in q]
    assert inserts, "код не записан"
    assert "expires_at" in inserts[0], (
        "код выдан без срока — он будет работать вечно, а отозвать его нечем"
    )
    assert re.search(r"INTERVAL\s*'\d+\s*days'", inserts[0]), (
        "срок должен считаться от момента выдачи"
    )


def test_join_filters_out_expired_codes():
    pool = _Pool(invite=None)
    assert _run(db.use_workspace_invite(pool, "код", 777)) is None

    selects = [q for q in pool.log if "workspace_invites" in q and "SELECT" in q]
    assert selects, "запрос приглашения не найден"
    sql = selects[0]
    assert "expires_at" in sql, "вход по коду не смотрит на срок — код вечен"
    assert "created_at" in sql, (
        "у старых строк срок пуст; без опоры на created_at они остаются вечными"
    )
    assert "uses_left" in sql, "счётчик входов проверять тоже надо"


def test_expired_code_lets_nobody_in():
    """Просроченный код не находится запросом — значит, никого не добавляет."""
    pool = _Pool(invite=None)

    assert _run(db.use_workspace_invite(pool, "старый", 777)) is None
    assert not [q for q in pool.log if "INSERT INTO workspace_members" in q], (
        "по просроченному коду добавили участника"
    )
