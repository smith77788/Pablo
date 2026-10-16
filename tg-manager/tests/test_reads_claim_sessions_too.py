"""Разовые чтения Telegram берут аккаунт у того же арбитра, что и операции.

`test_executors_claim_sessions` закрыл этот класс внутри `op_worker`: живую
сессию нельзя поднимать без захвата, иначе одна auth-key сессия коннектится из
двух мест. Но захват был только у массовых исполнителей, прогрева, призрака,
пре-флайта и живой консоли. Разовые чтения владельца — разбор аудитории
(`parser`), разбор сущности (`entity_analyzer`) и глобальный поиск
(`global_search_engine`) — брали аккаунт просто по выборке
(`get_best_account` / `select_all_active`), которая занятость не смотрит.

Процессный мьютекс сессии в `account_manager` это НЕ закрывает: роли
`INFRAGRAM_ROLE=web` и `worker` — разные процессы, карта занятости у каждого
своя, а единственный межпроцессный арбитр — аренда в БД (`op_worker`).
То есть владелец, открывший разбор канала во время массовой операции, уводил
ту же сессию вторым подключением.

Тесты проверяют три вещи: занятый аккаунт не берут, свободный берут, аренду
отпускают.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from services import entity_analyzer as ea
from services import global_search_engine as gse
from services import op_worker as opw
from services import parser as parser_mod
from services import flood_engine
from services import resource_selector


class _Arbiter:
    """Подмена арбитра: помнит, кого спрашивали и кого отпустили."""

    def __init__(self, busy: set[int] | None = None):
        self.busy = busy or set()
        self.asked: list[int] = []
        self.released: list[int] = []

    async def claim(self, acc_id: int) -> bool:
        self.asked.append(int(acc_id))
        return int(acc_id) not in self.busy

    async def release(self, ids) -> None:
        self.released.extend(int(i) for i in ids)


@pytest.fixture()
def arbiter(monkeypatch):
    a = _Arbiter()
    monkeypatch.setattr(opw, "try_claim_account", a.claim)
    monkeypatch.setattr(opw, "release_accounts", a.release)
    return a


# ── Глобальный поиск ─────────────────────────────────────────────────────────

class _NeverClient:
    async def connect(self):
        raise AssertionError("занятый аккаунт не должен подключаться")

    async def disconnect(self):
        return None


def test_global_search_refuses_a_busy_account(monkeypatch, arbiter):
    arbiter.busy = {5}
    monkeypatch.setattr("services.account_manager._make_client",
                        lambda *a, **k: _NeverClient())
    res = asyncio.run(gse.search_public("s", "дизайн", 10, _acc={"id": 5}))
    assert res["ok"] is False
    assert res.get("error_code") == "account_busy"
    assert arbiter.asked == [5]
    assert arbiter.released == [], "чего не захватили — того не отпускаем"


def test_global_search_message_is_russian(monkeypatch, arbiter):
    arbiter.busy = {5}
    monkeypatch.setattr("services.account_manager._make_client",
                        lambda *a, **k: _NeverClient())
    res = asyncio.run(gse.search_public("s", "дизайн", 10, _acc={"id": 5}))
    assert not re.search(r"[A-Za-z]{3,}", res["error"]), res["error"]


class _EmptyFound:
    chats: list = []
    users: list = []
    results: list = []


class _OkClient:
    def __init__(self):
        self.disconnected = False

    async def connect(self):
        return None

    async def __call__(self, _req):
        return _EmptyFound()

    async def disconnect(self):
        self.disconnected = True


def test_global_search_releases_the_account(monkeypatch, arbiter):
    client = _OkClient()
    monkeypatch.setattr("services.account_manager._make_client",
                        lambda *a, **k: client)
    res = asyncio.run(gse.search_public("s", "дизайн", 10, _acc={"id": 7}))
    assert res["ok"] is True
    assert arbiter.asked == [7]
    assert arbiter.released == [7], "аренда не отпущена — аккаунт припаркован"
    assert client.disconnected is True


def test_global_search_does_not_claim_twice(monkeypatch, arbiter):
    """Вызывающий, который уже держит аренду, не должен отказывать сам себе.

    Захват не реентрантный: исполнитель операции захватывает флот и лишь потом
    зовёт поиск. Повторный захват вернул бы False на СВОЙ же аккаунт.
    """
    client = _OkClient()
    monkeypatch.setattr("services.account_manager._make_client",
                        lambda *a, **k: client)
    res = asyncio.run(gse.search_public("s", "дизайн", 10, _acc={"id": 7},
                                        already_claimed=True))
    assert res["ok"] is True
    assert arbiter.asked == []
    assert arbiter.released == []


def test_the_only_caller_holding_a_lease_says_so():
    """Храповик: исполнитель `find_contact` захватывает флот до вызова поиска,
    поэтому обязан передавать already_claimed — иначе откажет сам себе."""
    src = inspect.getsource(opw)
    calls = [m for m in re.finditer(r"gse\.search_public\((?:[^()]|\([^()]*\))*\)", src)]
    assert calls, "вызов поиска из исполнителя пропал — проверьте храповик"
    for m in calls:
        assert "already_claimed=True" in m.group(0), m.group(0)


# ── Разбор сущности ──────────────────────────────────────────────────────────

class _AnalyzerClient:
    def __init__(self, acc_id):
        self.acc_id = acc_id
        self.disconnected = False

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True


def test_analyzer_skips_a_busy_account_and_takes_the_next(monkeypatch, arbiter):
    arbiter.busy = {1}
    made: list[int] = []

    async def _candidates(*a, **k):
        return [{"id": 1, "session_str": "a"}, {"id": 2, "session_str": "b"}]

    def _make(session, acc, *a, **k):
        made.append(int(acc["id"]))
        return _AnalyzerClient(int(acc["id"]))

    monkeypatch.setattr(resource_selector, "select_all_active", _candidates)
    monkeypatch.setattr("services.account_manager._make_client", _make)
    client = asyncio.run(ea._get_client(object(), 42))
    assert arbiter.asked == [1, 2]
    assert made == [2], "занятый аккаунт всё равно подключили"
    assert client.acc_id == 2
    assert getattr(client, "_infragram_acc_id", 0) == 2


def test_analyzer_releases_when_connect_fails(monkeypatch, arbiter):
    class _Dead(_AnalyzerClient):
        async def connect(self):
            raise ConnectionError("нет сети")

    async def _candidates(*a, **k):
        return [{"id": 3, "session_str": "a"}]

    monkeypatch.setattr(resource_selector, "select_all_active", _candidates)
    monkeypatch.setattr("services.account_manager._make_client",
                        lambda s, acc, *a, **k: _Dead(int(acc["id"])))
    assert asyncio.run(ea._get_client(object(), 42)) is None
    assert arbiter.released == [3], (
        "неудачный коннект оставил аренду — аккаунт припаркован до её истечения"
    )


def test_analyzer_release_helper_reads_the_client_id(arbiter):
    client = _AnalyzerClient(9)
    client._infragram_acc_id = 9
    asyncio.run(ea._release_client_lease(client))
    assert arbiter.released == [9]


def test_every_analysis_entry_point_releases_the_lease():
    """Храповик: у каждой точки входа разбора аренда отпускается в finally."""
    src = inspect.getsource(ea)
    assert src.count("await _release_client_lease(client)") == 3, (
        "точка входа разбора перестала отпускать аренду"
    )


# ── Разбор аудитории ─────────────────────────────────────────────────────────

def test_parser_takes_the_next_account_when_the_best_is_busy(monkeypatch, arbiter):
    arbiter.busy = {11}
    seen_excludes: list[list[int]] = []

    async def _best(pool, owner_id, action_type="default", exclude_ids=None, **k):
        seen_excludes.append(list(exclude_ids or []))
        for aid in (11, 12):
            if aid not in (exclude_ids or []):
                return {"id": aid, "session_str": "s"}
        return None

    monkeypatch.setattr(flood_engine, "get_best_account", _best)
    acc, leased = asyncio.run(parser_mod._claim_best_account(object(), 1))
    assert acc["id"] == 12
    assert leased == 12
    assert seen_excludes == [[], [11]], "занятый аккаунт не исключили из выбора"


def test_parser_gives_up_honestly_when_everyone_is_busy(monkeypatch, arbiter):
    arbiter.busy = {11, 12}

    async def _best(pool, owner_id, action_type="default", exclude_ids=None, **k):
        for aid in (11, 12):
            if aid not in (exclude_ids or []):
                return {"id": aid, "session_str": "s"}
        return None

    monkeypatch.setattr(flood_engine, "get_best_account", _best)
    acc, leased = asyncio.run(parser_mod._claim_best_account(object(), 1))
    assert acc is None and leased == 0


def test_parser_does_not_loop_forever(monkeypatch, arbiter):
    """Флот из сотни занятых аккаунтов не должен перебираться целиком."""
    arbiter.busy = set(range(1, 200))
    counter = {"n": 0}

    async def _best(pool, owner_id, action_type="default", exclude_ids=None, **k):
        counter["n"] += 1
        return {"id": counter["n"], "session_str": "s"}

    monkeypatch.setattr(flood_engine, "get_best_account", _best)
    acc, _ = asyncio.run(parser_mod._claim_best_account(object(), 1))
    assert acc is None
    assert counter["n"] == parser_mod._CLAIM_TRIES


def test_every_parse_entry_point_releases_the_lease():
    src = inspect.getsource(parser_mod)
    assert src.count("await _release_account(_leased)") == 3
    assert src.count("await _claim_best_account(pool, owner_id)") == 3
