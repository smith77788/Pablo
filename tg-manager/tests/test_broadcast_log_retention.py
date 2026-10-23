"""Журнал доставки рассылок чистится, но повтор от этого не превращается в спам.

`broadcast_delivery_log` — строка на каждого получателя каждой рассылки, и он
не чистился вообще. Чистить по сроку напрямую нельзя: «отправить
недоставленным» считает недоставленными тех, кого НЕТ в журнале, значит пустой
журнал означает повторную отправку ВСЕМ подписчикам — спам живым людям.

Поэтому уборка сначала помечает рассылку (`delivery_log_pruned`), и только
потом удаляет её строки, а повтор по помеченной рассылке отказывает. Здесь
проверяется и отказ, и порядок двух запросов уборки: обратный порядок — это
ровно тот спам.
"""
from __future__ import annotations

import pytest

from services import broadcaster, db_maintenance


class _ПулПовтора:
    """Минимальный пул для resend_undelivered."""

    def __init__(self, src):
        self._src = src
        self.inserted_broadcast = None

    async def fetchrow(self, q, *a):
        if "FROM broadcasts WHERE id=" in q:
            return self._src
        if "FROM managed_bots" in q:
            return {"bot_id": 10}
        if q.startswith("INSERT INTO broadcasts"):
            self.inserted_broadcast = a
            return {"id": 777}
        return None

    async def fetch(self, q, *a):
        if "FROM bot_users" in q:
            return [{"user_id": 100}, {"user_id": 101}]
        return []

    async def fetchval(self, q, *a):
        return None


@pytest.mark.asyncio
async def test_повтор_по_очищенной_рассылке_отказывает():
    """Журнал удалён — повтор запрещён, новая рассылка не создаётся."""
    pool = _ПулПовтора({
        "id": 1, "bot_id": 10, "message_text": "привет", "created_by": 42,
        "delivery_log_pruned": True,
    })
    res = await broadcaster.resend_undelivered(pool, owner_id=42, bc_id=1)
    assert res["ok"] is False, res
    assert res["code"] == 409, res
    assert pool.inserted_broadcast is None, "рассылка не должна создаваться"
    # Текст виден владельцу, а он читает только по-русски.
    assert "журнал доставки" in res["error"].lower(), res["error"]


@pytest.mark.asyncio
async def test_целый_журнал_повтору_не_мешает():
    """Тот же путь при живом журнале работает как раньше."""
    pool = _ПулПовтора({
        "id": 1, "bot_id": 10, "message_text": "привет", "created_by": 42,
        "delivery_log_pruned": False,
    })
    res = await broadcaster.resend_undelivered(pool, owner_id=42, bc_id=1)
    # Дальше идёт постановка операции, которую здесь не мокаем: важно лишь, что
    # отказа по журналу нет.
    assert res.get("code") != 409, res


class _ПулУборки:
    """Записывает порядок запросов уборки."""

    def __init__(self):
        self.запросы: list[str] = []

    async def fetch(self, q, *a):
        self.запросы.append(q)
        if "FROM broadcasts" in q:
            return [{"id": 5}, {"id": 6}]
        return []

    async def execute(self, q, *a):
        self.запросы.append(q)
        return "UPDATE 2"

    async def fetchval(self, q, *a):
        self.запросы.append(q)
        return 0


@pytest.mark.asyncio
async def test_отметка_ставится_до_удаления_журнала():
    """Порядок обязателен: сначала отметка, потом DELETE.

    Если проход прервётся между ними, запрет повтора окажется на рассылке с
    целым журналом — это безопасно. Обратный порядок оставил бы рассылку без
    журнала и без запрета, то есть повтор ушёл бы всем подписчикам.
    """
    pool = _ПулУборки()
    deleted, err = await db_maintenance._prune_broadcast_delivery_log(pool)
    assert err is None, err

    отметка = next(i for i, q in enumerate(pool.запросы)
                   if "delivery_log_pruned = TRUE" in q)
    удаление = next(i for i, q in enumerate(pool.запросы)
                    if "DELETE FROM broadcast_delivery_log" in q)
    assert отметка < удаление, pool.запросы


@pytest.mark.asyncio
async def test_без_подходящих_рассылок_уборка_ничего_не_пишет():
    """Нет старых завершённых рассылок — ни отметок, ни удалений."""

    class _Пусто(_ПулУборки):
        async def fetch(self, q, *a):
            self.запросы.append(q)
            return []

    pool = _Пусто()
    deleted, err = await db_maintenance._prune_broadcast_delivery_log(pool)
    assert (deleted, err) == (0, None)
    assert not any("delivery_log_pruned = TRUE" in q for q in pool.запросы)


def test_идущая_рассылка_из_уборки_исключена():
    """Журнал идущей рассылки нужен для возобновления после падения."""
    assert "pending" not in db_maintenance._BROADCAST_DONE_STATUSES
    assert "sending" not in db_maintenance._BROADCAST_DONE_STATUSES
    assert "done" in db_maintenance._BROADCAST_DONE_STATUSES
