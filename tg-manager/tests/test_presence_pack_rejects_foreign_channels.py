"""В свой Presence Pack нельзя вписать чужой канал.

Состав пакета приходит из тела запроса мини-аппа. Рядом, для `bot_id`, дверь
проверяет владение явно («иначе — линковка чужого bot_id в свой пак»), а номера
каналов писались как есть. Дальше посев начальных постов
(`_exec_seed_presence_pack`) читает по этим номерам title, username, channel_id
и access_hash и пытается публиковать. access_hash — это ключ, которым Telegram
резолвит канал, в том числе закрытый: чужим его отдавать нельзя.

Проверка стоит в самой функции записи, а не в обработчике: дверей две — мини-апп
и бот, — и они расходятся (так уже терялся гард на удалении прокси).

Отказ целиком, без частичной записи: молча выкинуть половину состава и сказать
«сохранено» — худший из вариантов, пакет потом сеет не туда, куда человек
выбрал.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


class _Pool:
    """Своими считает каналы из `own`; всё остальное — чужое или удалённое."""

    def __init__(self, own: set[int]):
        self._own = own
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *a):
        assert "owner_id=$1" in sql, f"выборка своих каналов без владельца: {sql}"
        ids = a[1]
        return [{"id": i} for i in ids if i in self._own]

    async def execute(self, sql, *a):
        self.executed.append((" ".join(sql.split()), a))
        return "UPDATE 1"

    async def fetchrow(self, sql, *a):
        return None


def _run(pool, channel_ids, group_ids=(), owner_id=111, pack_id=7):
    return asyncio.run(
        db.update_presence_pack_channels(
            pool, pack_id, owner_id, list(channel_ids), list(group_ids)
        )
    )


def _записи_состава(pool: _Pool) -> list:
    return [sql for sql, _ in pool.executed if "presence_packs" in sql]


def test_свой_состав_сохраняется():
    pool = _Pool(own={1, 2, 3})
    res = _run(pool, [1, 2], [3])
    assert res["ok"] is True and res["foreign"] == []
    assert _записи_состава(pool), "состав своих каналов не сохранён"


def test_чужой_канал_отклоняется_целиком():
    pool = _Pool(own={1, 2})
    res = _run(pool, [1, 2, 99])
    assert res["ok"] is False
    assert res["foreign"] == [99]
    assert not _записи_состава(pool), (
        "состав записан несмотря на чужой канал — посев уйдёт по чужому "
        "access_hash"
    )


def test_чужая_группа_тоже_отклоняется():
    pool = _Pool(own={1})
    res = _run(pool, [1], [77])
    assert res["ok"] is False and res["foreign"] == [77]
    assert not _записи_состава(pool)


def test_отказ_остаётся_в_журнале():
    pool = _Pool(own=set())
    _run(pool, [42])
    журнал = [sql for sql, _ in pool.executed if "operation_audit" in sql]
    assert журнал, "отказ не записан в operation_audit"
    args = [a for sql, a in pool.executed if "operation_audit" in sql][0]
    assert "presence_pack_foreign_channel_refused" in args
    assert "refused" in args


def test_пустой_состав_разрешён():
    # «Убрать все каналы» — законное действие, лишних запросов оно не требует.
    pool = _Pool(own={1})
    res = _run(pool, [], [])
    assert res["ok"] is True
    assert _записи_состава(pool)
