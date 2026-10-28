"""Авторазметка сегментов не должна ходить в базу по разу на подписчика.

`db.autotag_by_activity` проставляет каждому подписчику бота тег
`activity:hot/warm/cold/lost`. Раньше она вычитывала четыре списка user_id и
шла по ним циклом, делая на каждого подписчика DELETE и INSERT — 2N
последовательных обращений к базе, каждое своей транзакцией.

Чем это плохо на реальном боте:

* время растёт линейно с числом подписчиков, и на десятках тысяч проход
  просто не доезжает до конца (таймаут, перезапуск контейнера, деплой);
* оборвавшись на середине, он оставляет разметку наполовину применённой:
  у части подписчиков старый тег уже снят, а новый не проставлен, и
  следующая сегментная рассылка уходит не тем людям;
* всё это время занята соединение из пула.

Тест держит два свойства: число запросов не зависит от числа подписчиков,
и вся переразметка идёт одной транзакцией.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


class _Conn:
    def __init__(self, pool):
        self._pool = pool
        self._depth = 0

    def transaction(self):
        return self

    async def __aenter__(self):
        self._depth += 1
        return self

    async def __aexit__(self, *a):
        self._depth -= 1
        return False

    async def execute(self, sql, *a):
        self._pool.writes.append((sql, self._depth > 0))
        return "INSERT 0 0"

    async def fetch(self, sql, *a):
        return await self._pool.fetch(sql, *a)


class _FakePool:
    """Пул, который считает записи и помнит, были ли они в транзакции."""

    def __init__(self, user_count: int):
        self.writes: list[tuple[str, bool]] = []
        self.reads: list[str] = []
        self._users = [{"user_id": 1000 + i} for i in range(user_count)]

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool)

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def fetch(self, sql, *a):
        self.reads.append(sql)
        if "COUNT(*)" in sql:
            return [{"hot": 1, "warm": 0, "cold": 0, "lost": 0, "total": 1}]
        return list(self._users)

    async def fetchval(self, sql, *a):
        return None

    async def fetchrow(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        # Запись мимо соединения — значит, мимо транзакции.
        self.writes.append((sql, False))
        return "INSERT 0 0"


def _run(user_count: int) -> _FakePool:
    pool = _FakePool(user_count)
    asyncio.run(db.autotag_by_activity(pool, 777))
    return pool


def test_число_запросов_не_растёт_с_числом_подписчиков():
    мало = _run(3)
    много = _run(3000)
    assert len(мало.writes) == len(много.writes), (
        "разметка ходит в базу по разу на подписчика: "
        f"{len(мало.writes)} запросов на 3 подписчиках и "
        f"{len(много.writes)} на 3000"
    )
    assert len(много.writes) <= 4, (
        f"на переразметку ушло {len(много.writes)} запросов, ожидались единицы"
    )


def test_переразметка_идёт_одной_транзакцией():
    pool = _run(50)
    вне = [sql for sql, в_транзакции in pool.writes if not в_транзакции]
    assert not вне, (
        "запись вне транзакции — обрыв оставит подписчиков без тега: "
        f"{вне[:2]}"
    )


def test_границы_сегментов_сохранены():
    pool = _run(5)
    вставки = [sql for sql, _ in pool.writes if "INSERT" in sql.upper()]
    assert len(вставки) == 1, "ожидался один INSERT на всю разметку"
    sql = вставки[0]
    for тег in ("activity:hot", "activity:warm", "activity:cold", "activity:lost"):
        assert тег in sql, f"сегмент {тег} потерян"
    for граница in ("INTERVAL '1 day'", "INTERVAL '7 days'", "INTERVAL '30 days'"):
        assert граница in sql, f"граница {граница} потеряна"
    assert "ON CONFLICT" in sql, "повторный прогон должен быть безопасен"

    удаления = [sql for sql, _ in pool.writes if sql.strip().upper().startswith("DELETE")]
    assert len(удаления) == 1 and "activity:%" in удаления[0], (
        "старые теги activity: должны сниматься одним запросом"
    )
