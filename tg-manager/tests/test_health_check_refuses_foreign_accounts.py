"""Проверка здоровья не должна работать чужими сессиями.

Список аккаунтов приходит исполнителю в params — то есть в том виде, в каком
его записала дверь. А проверка здоровья не читает базу: она ПОДКЛЮЧАЕТСЯ к
Telegram настоящей сессией аккаунта, узнаёт его статус, может сменить
acc_status и деактивировать. Чужой id в списке — это работа чужой сессией и
чужие данные в ответе.

Раньше исполнитель по явному списку фильтровал только по id, без владельца:
любая дверь, забывшая скоуп, открывала доступ к чужим аккаунтам. Двери на
сегодня скоуп соблюдают, но проверять это в каждой из них — тот же путь, на
котором продукт уже терял защиту (см. удаление прокси: гард был в мини-аппе и
не был в боте). Поэтому отказ живёт в самом исполнителе.

Исключение — платформенный срез админа: дверь проверяет права и ставит в
params признак cross_owner. Без него админская проверка чужих аккаунтов
давала 0 строк и операция падала «0/N», поэтому признак и проверяется тестом.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker


class _Pool:
    def __init__(self, foreign: int = 0):
        self.queries: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self._foreign = foreign

    async def fetch(self, sql, *a):
        self.queries.append((" ".join(sql.split()), a))
        return []

    async def fetchval(self, sql, *a):
        self.queries.append((" ".join(sql.split()), a))
        return self._foreign

    async def execute(self, sql, *a):
        self.executed.append((" ".join(sql.split()), a))
        return "INSERT 0 1"


def _run(params, *, owner_id=111, foreign=0) -> _Pool:
    pool = _Pool(foreign=foreign)
    res = asyncio.run(
        op_worker._exec_check_accounts_health(pool, None, 42, owner_id, params)
    )
    # Аккаунтов фейковый пул не отдаёт — исполнитель выходит до Telethon.
    assert res["status"] == "failed"
    return pool


def _запрос_аккаунтов(pool: _Pool) -> tuple[str, tuple]:
    for sql, args in pool.queries:
        if "FROM tg_accounts a" in sql:
            return sql, args
    raise AssertionError("исполнитель не запрашивал аккаунты")


def test_чужие_id_без_признака_отсекаются_по_владельцу():
    pool = _run({"account_ids": [7, 8, 9]}, owner_id=111)
    sql, args = _запрос_аккаунтов(pool)
    assert "a.owner_id=" in sql, (
        "выборка аккаунтов идёт без владельца — чужой id из params даст работу "
        "чужой сессией"
    )
    assert 111 in args


def test_платформенный_срез_админа_не_сломан():
    pool = _run({"account_ids": [7, 8, 9], "cross_owner": True}, owner_id=111)
    sql, _ = _запрос_аккаунтов(pool)
    assert "a.owner_id=" not in sql, (
        "с признаком cross_owner фильтр по владельцу вернул бы 0 строк — "
        "именно так операция падала «0/N»"
    )


def test_без_списка_берутся_только_свои():
    pool = _run({}, owner_id=222)
    sql, args = _запрос_аккаунтов(pool)
    assert "a.owner_id=" in sql and 222 in args


def test_попытка_взять_чужие_аккаунты_остаётся_в_журнале():
    pool = _run({"account_ids": [7, 8]}, owner_id=111, foreign=2)
    записи = [sql for sql, _ in pool.executed if "operation_audit" in sql]
    assert записи, "отказ не записан в operation_audit — разобрать инцидент будет нечем"
    args = [a for sql, a in pool.executed if "operation_audit" in sql][0]
    assert "foreign_accounts_refused" in args, "в журнале не то действие"
    assert "refused" in args


def test_своим_аккаунтам_журнал_отказов_не_пишется():
    pool = _run({"account_ids": [7, 8]}, owner_id=111, foreign=0)
    записи = [sql for sql, _ in pool.executed if "operation_audit" in sql]
    assert not записи, "журнал отказов пишется на обычной операции — будет шум"
