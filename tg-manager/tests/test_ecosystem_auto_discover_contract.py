"""Автоподбор в экосистему: работает и отдаёт контракт, который рисуют экраны.

Почему тест появился отдельно. `auto_discover_members` была объявлена в
`ecosystem_brain` ДВАЖДЫ (второе объявление — коммит от 2026-07-02), и работала
вторая версия с другим контрактом: `{added, skipped, total}` вместо
`{тип_объекта: сколько_добавлено}`. Все три вызывающих экрана написаны под
первый контракт:

* `bot/handlers/ecosystems.py` собирает строку по `_MEMBER_TYPES`, то есть
  ищет в ответе ключи `account` / `channel` / `bot` — и не находил ни одного,
  поэтому экран ВСЕГДА показывал «объекты добавлены вручную», сколько бы
  объектов ни добавилось;
* мини-апп считал `sum(added.values())` — по `{added, skipped, total}` это даёт
  бессмысленное число (сложение добавленных, пропущенных и их суммы).

Дубль снят, но тестов у функции не было вовсе — поэтому два с половиной месяца
никто и не заметил. Здесь закреплён именно КОНТРАКТ: ключи ответа обязаны быть
типами объектов, которые экраны умеют рисовать.
"""
from __future__ import annotations

import asyncio

import pytest

from services import ecosystem_brain as eb


class _Pool:
    """Пул, отвечающий по смыслу запроса: три выборки идут разными таблицами."""

    def __init__(self, accounts=(), channels=(), bots=()):
        self._by_table = {
            "tg_accounts": [{"id": i} for i in accounts],
            "managed_channels": [{"id": i} for i in channels],
            "managed_bots": [{"id": i} for i in bots],
        }
        self.added: list[tuple[str, int]] = []

    async def fetchrow(self, sql, *args):
        if "FROM ecosystems" in sql:
            return {"id": args[0], "owner_id": args[1], "region": None, "name": "тест"}
        return None

    async def fetch(self, sql, *args):
        for table, rows in self._by_table.items():
            if table in sql:
                return rows
        return []

    async def execute(self, *a, **k):
        return None


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def no_ownership_check(monkeypatch):
    """add_member по-настоящему ходит в БД за проверкой владения — она не наш предмет."""
    async def _ok(pool, owner_id, object_type, object_id):
        return False        # объект не чужой

    monkeypatch.setattr(eb, "object_belongs_to_someone_else", _ok)


@pytest.fixture
def capture_add(monkeypatch):
    calls: list[tuple[str, int]] = []

    async def _add(pool, ecosystem_id, owner_id, object_type, object_id, role="member"):
        calls.append((object_type, object_id))
        return True

    monkeypatch.setattr(eb, "add_member", _add)
    return calls


def test_keys_are_object_types_the_screens_can_render(capture_add):
    """Ответ рисуется через _MEMBER_TYPES — ключи обязаны быть оттуда.

    Это и есть проверка, которую не прошла бы вторая (затенявшая) версия:
    её ключи `added`/`skipped`/`total` в _MEMBER_TYPES не входят, поэтому экран
    показывал пустую строку.
    """
    from bot.handlers.ecosystems import _MEMBER_TYPES

    pool = _Pool(accounts=[1, 2], channels=[10], bots=[100, 101, 102])
    res = _run(eb.auto_discover_members(pool, 1, 777))

    assert res, "автоподбор ничего не вернул, хотя объекты были"
    unknown = set(res) - set(_MEMBER_TYPES)
    assert not unknown, (
        f"ключи {sorted(unknown)} экран нарисовать не умеет — "
        f"он знает только {sorted(_MEMBER_TYPES)}"
    )


def test_counts_are_per_type_and_honest(capture_add):
    pool = _Pool(accounts=[1, 2], channels=[10], bots=[100, 101, 102])
    res = _run(eb.auto_discover_members(pool, 1, 777))

    assert res == {"account": 2, "channel": 1, "bot": 3}, res
    # сумма — это именно «сколько добавлено», как её и считает мини-апп
    assert sum(res.values()) == 6


def test_all_three_kinds_are_discovered(capture_add):
    """Аккаунты, каналы И боты — обе прежние версии вместе, а не одна из них."""
    pool = _Pool(accounts=[1], channels=[10], bots=[100])
    _run(eb.auto_discover_members(pool, 1, 777))

    assert {t for t, _ in capture_add} == {"account", "channel", "bot"}


def test_nothing_to_add_gives_empty_not_zeroes(capture_add):
    """Пусто — это {}, а не {account: 0}: экран не должен писать «добавлено 0»."""
    res = _run(eb.auto_discover_members(_Pool(), 1, 777))
    assert res == {}


def test_unknown_ecosystem_is_refused(capture_add):
    class _NoEco(_Pool):
        async def fetchrow(self, sql, *args):
            return None

    assert _run(eb.auto_discover_members(_NoEco(), 999, 777)) == {}
    assert not capture_add, "чужая или несуществующая экосистема не должна ничего добавлять"


def test_already_present_objects_are_not_counted(no_ownership_check, monkeypatch):
    """add_member вернул False (объект уже в экосистеме) — в счётчик не идёт."""
    async def _add(pool, ecosystem_id, owner_id, object_type, object_id, role="member"):
        return object_type == "channel"      # добавился только канал

    monkeypatch.setattr(eb, "add_member", _add)
    res = _run(eb.auto_discover_members(_Pool(accounts=[1], channels=[10], bots=[100]), 1, 777))
    assert res == {"channel": 1}, res
