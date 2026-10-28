"""Цикл дрейфа карточек: чужой сессией не подключаться, отметки — одним запросом.

Два класса проблем, оба проверяются поведением, а не текстом исходника.

1. **Скоуп по владельцу.** `managed_channels.acc_id` — это аккаунт, которым
   канал когда-то заводили. Если он разойдётся с владельцем канала (перенос,
   правка строки руками, баг в заведении), выборка аккаунта без `owner_id`
   отдаст аккаунт ДРУГОГО владельца, и цикл пойдёт в Telegram чужой сессией.
   Запасной аккаунт в том же коде выбирается строго по `owner_id` — значит
   замысел именно такой.

2. **Отметка осмотра одним запросом.** Отметка по строке — отдельное обращение
   к базе на каждый канал (до 40 за группу), и всё это внутри цикла, который
   держит захваченную сессию аккаунта.
"""
from __future__ import annotations

import pytest

from services import drift_detector as dd


class _Pool:
    """Заглушка пула, которая ЧИТАЕТ выданный ей SQL.

    Условия она применяет те, что в запросе действительно стоят: запрос без
    `owner_id` отдаст аккаунт чужого владельца — ровно как настоящий Postgres.
    """

    def __init__(self, channels: list[dict], accounts: dict[int, dict]) -> None:
        self.channels = channels
        self.accounts = accounts
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        if "FROM managed_channels" in sql:
            return self.channels
        return []

    async def fetchrow(self, sql, *args):
        if "FROM tg_accounts" not in sql:
            return None
        if "WHERE id=$1" in sql:
            acc = self.accounts.get(args[0])
            if acc is None:
                return None
            if "owner_id=$2" in sql and acc["owner_id"] != args[1]:
                return None
            return acc
        for acc in self.accounts.values():          # запасной — по владельцу
            if acc["owner_id"] == args[0]:
                return acc
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _channel(cid: int, owner_id: int, acc_id: int) -> dict:
    return {"id": cid, "owner_id": owner_id, "acc_id": acc_id,
            "channel_id": 1000 + cid, "title": "Канал", "username": "",
            "about": "", "access_hash": 0}


def _account(aid: int, owner_id: int) -> dict:
    return {"id": aid, "owner_id": owner_id, "session_str": f"sess-{aid}",
            "is_active": True, "acc_status": "active", "trust_score": 50}


@pytest.fixture
def _stubs(monkeypatch):
    """Сессии не открываем: запоминаем, с какой строкой сессии позвали бы."""
    from services import account_manager, op_worker

    used: list[str] = []

    async def _fake_info(session_str, ids, **kw):
        used.append(session_str)
        return {}

    monkeypatch.setattr(account_manager, "get_channels_full_info", _fake_info)
    monkeypatch.setattr(op_worker, "try_claim_account",
                        lambda *a, **k: _true())
    monkeypatch.setattr(op_worker, "release_accounts",
                        lambda *a, **k: _none())
    return used


async def _true():
    return True


async def _none():
    return None


@pytest.mark.asyncio
async def test_does_not_connect_with_another_owners_session(_stubs):
    """acc_id указывает на аккаунт ЧУЖОГО владельца — сессия не открывается."""
    pool = _Pool([_channel(1, owner_id=10, acc_id=7)],
                 {7: _account(7, owner_id=20)})

    await dd._check_all(pool, bot=None)

    assert _stubs == [], (
        "цикл подключился сессией другого владельца — выборка аккаунта не "
        "скоупится по owner_id")


@pytest.mark.asyncio
async def test_own_account_is_still_used(_stubs):
    """Здоровый случай не должен пострадать: свой аккаунт берётся как раньше."""
    pool = _Pool([_channel(1, owner_id=10, acc_id=7)],
                 {7: _account(7, owner_id=10)})

    await dd._check_all(pool, bot=None)

    assert _stubs == ["sess-7"]


@pytest.mark.asyncio
async def test_unreachable_channels_are_stamped_once(_stubs):
    """Каналы без живой сессии владельца помечаются осмотренными одним запросом,
    иначе они вечно забивают выборку из 200 строк."""
    pool = _Pool([_channel(1, 10, 7), _channel(2, 10, 7)],
                 {7: _account(7, owner_id=20)})

    await dd._check_all(pool, bot=None)

    stamps = [q for q in pool.executed if "last_drift_check" in q[0]]
    assert len(stamps) == 1, f"отметка пишется не одним запросом: {len(stamps)}"
    assert sorted(stamps[0][1][0]) == [1, 2]


@pytest.mark.asyncio
async def test_stamps_are_written_in_one_query_for_the_whole_batch(_stubs):
    """Пять каналов — одна отметка, а не пять round-trip'ов."""
    chans = [_channel(i, 10, 7) for i in range(1, 6)]
    pool = _Pool(chans, {7: _account(7, owner_id=10)})

    await dd._check_all(pool, bot=None)

    stamps = [q for q in pool.executed if "last_drift_check" in q[0]]
    assert len(stamps) == 1, (
        f"отметка осмотра пишется по строке: {len(stamps)} запросов на 5 каналов")
    assert sorted(stamps[0][1][0]) == [1, 2, 3, 4, 5]
