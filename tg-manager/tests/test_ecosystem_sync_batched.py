"""Синхронизация экосистемы: один запрос на тип, а не на участника.

`sync_ecosystem_members` проверяла каждого участника отдельным `fetchrow` и
удаляла выпавших по одному `DELETE`. Экосистема на триста аккаунтов — это
триста обращений к базе подряд внутри запроса владельца.

Второе, более важное: выборка теперь общая, и её сбой НЕ должен выглядеть как
«участников больше нет» — иначе одна ошибка базы вычистила бы экосистему
целиком. Тип с упавшей выборкой пропускается, удаления не происходит.
"""
from __future__ import annotations

import pytest

from services import ecosystem_brain as eb


class _Pool:
    def __init__(self, tables: dict[str, list[dict]], fail: set[str] | None = None):
        self.tables = tables
        self.fail = fail or set()
        self.fetches: list[str] = []
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        self.fetches.append(sql)
        for name, rows in self.tables.items():
            if f"FROM {name}" in sql:
                if name in self.fail:
                    raise RuntimeError("выборка упала")
                ids = set(args[0])
                hit = [r for r in rows if r[next(iter(r))] in ids]
                # Настоящий запрос переименовывает колонку в oid — заглушка
                # обязана отдавать то же имя, иначе «проверка» мерила бы себя.
                if " AS oid" in sql:
                    return [{"oid": r[next(iter(r))]} for r in hit]
                return hit
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "DELETE 1"

    async def fetchrow(self, sql, *args):
        raise AssertionError("поштучная выборка вернулась — снова N+1")

    async def fetchval(self, sql, *args):
        return None


@pytest.fixture
def _members(monkeypatch):
    """get_members и record_event — не предмет этого теста."""
    rows: list[dict] = []

    async def _get_members(pool, eco_id):
        return rows

    async def _record_event(*a, **kw):
        return None

    monkeypatch.setattr(eb, "get_members", _get_members)
    monkeypatch.setattr(eb, "record_event", _record_event)
    return rows


@pytest.mark.asyncio
async def test_one_query_per_type_not_per_member(_members):
    _members.extend({"object_type": "account", "object_id": i} for i in range(1, 21))
    pool = _Pool({"tg_accounts": [
        {"id": i, "is_active": True, "acc_status": "active",
         "trust_score": 0.9, "label": f"acc{i}"} for i in range(1, 21)]})

    res = await eb.sync_ecosystem_members(pool, 1, owner_id=10)

    assert res["ok"] == 20 and res["removed"] == 0
    assert len(pool.fetches) == 1, (
        f"на 20 участников одного типа ушло {len(pool.fetches)} запросов")


@pytest.mark.asyncio
async def test_missing_members_are_deleted_in_one_statement(_members):
    _members.extend({"object_type": "account", "object_id": i} for i in range(1, 6))
    pool = _Pool({"tg_accounts": [
        {"id": 1, "is_active": True, "acc_status": "active",
         "trust_score": 0.9, "label": "acc1"}]})

    res = await eb.sync_ecosystem_members(pool, 1, owner_id=10)

    assert res["removed"] == 4 and res["ok"] == 1
    assert len(pool.executed) == 1, (
        f"удаление пишется по строке: {len(pool.executed)} запросов на 4 выпавших")
    assert sorted(pool.executed[0][1][2]) == [2, 3, 4, 5]


@pytest.mark.asyncio
async def test_prefetch_failure_deletes_nothing(_members):
    """Сбой выборки — не повод вычистить экосистему."""
    _members.extend({"object_type": "account", "object_id": i} for i in range(1, 6))
    pool = _Pool({"tg_accounts": []}, fail={"tg_accounts"})

    res = await eb.sync_ecosystem_members(pool, 1, owner_id=10)

    assert pool.executed == [], "участники удалены из-за ошибки базы"
    assert res["removed"] == 0 and res["ok"] == 0 and res["stale"] == []


@pytest.mark.asyncio
async def test_channel_found_in_either_table_survives(_members):
    """Канал живёт в managed_channels ИЛИ в tg_channels — объединение, как было."""
    _members.extend([
        {"object_type": "channel", "object_id": 100},
        {"object_type": "channel", "object_id": 200},
        {"object_type": "group", "object_id": 300},
    ])
    pool = _Pool({
        "managed_channels": [{"channel_id": 100}],
        "tg_channels": [{"id": 200}],
    })

    res = await eb.sync_ecosystem_members(pool, 1, owner_id=10)

    assert res["ok"] == 2, "канал из одной из двух таблиц потерян"
    assert res["removed"] == 1
    assert sorted(pool.executed[0][1][2]) == [300]


@pytest.mark.asyncio
async def test_stale_account_is_reported_not_deleted(_members):
    """Забаненный аккаунт — «устаревший», а не отсутствующий."""
    _members.append({"object_type": "account", "object_id": 1})
    pool = _Pool({"tg_accounts": [
        {"id": 1, "is_active": True, "acc_status": "banned",
         "trust_score": 0.9, "label": "acc1"}]})

    res = await eb.sync_ecosystem_members(pool, 1, owner_id=10)

    assert res["removed"] == 0 and pool.executed == []
    assert [s["issue"] for s in res["stale"]] == ["заблокирован"]
