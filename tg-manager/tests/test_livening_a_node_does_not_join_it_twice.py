"""Оживление ноды не должно вступать в неё второй раз.

«Оживить ноду» — это N аккаунтов флота вступают в её супергруппу. Повтор
прогона здесь норма, а не редкость: рабочая ветка едет в прод на каждом пуше,
рестарт возвращает операцию в очередь, и прогон идёт по аккаунтам с начала.

Двери не было вовсе, а в реестре идемпотентности стояла запись «повтор не
вредит» с такой причиной: «Повтор может привести в ноду ЛИШНИЕ аккаунты сверх
count — это ограничено потолком 50 и не создаёт ничего видимого людям». Но
правило того же реестра называет вредом ровно это: «второй joinChannel в счёт
лимита». То есть причина признавала тот самый вред, от которого должна была
освобождать.

Цена была двойная. Во-первых, до пятидесяти настоящих `joinChannel` за повтор,
а вступление — действие баноопасное и лимитируемое. Во-вторых, выбор
аккаунтов шёл заново и мог взять ДРУГИЕ: владелец просил пять, а после одного
воскрешения в ноде оказывалось десять.

Дверь естественная и уже была в базе: `community_node_members` — кто в ноде
состоит. Теперь прогон читает её, исключает своих же из выбора и доводит ноду
ДО `count`, а не добавляет ещё `count`. Нода уже полна — не вступает никто, и
владелец читает об этом словами.
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


class _Pool:
    """Стенд базы: в ноде уже состоят `members`."""

    def __init__(self, members=()):
        self.members = [int(x) for x in members]

    async def fetch(self, sql, *args):
        if "community_node_members" in sql:
            return [{"account_id": a} for a in self.members]
        return []

    async def fetchrow(self, sql, *args):
        if "community_nodes" in sql:
            return {"tg_chat_id": -1001234}
        return None

    async def execute(self, sql, *args):
        return "UPDATE 1"

    async def fetchval(self, sql, *args):
        return None


class _Inv:
    invite_link = "https://t.me/+stand"


class _Bot:
    async def create_chat_invite_link(self, chat_id):
        return _Inv()


@pytest.fixture
def stand(monkeypatch):
    """Подменяем всё, что исполнитель зовёт вокруг выбора и вступления."""
    from services import account_manager, nodes_engine, op_worker, resource_selector

    asked: list[dict] = []
    joins: list[int] = []
    members_added: list[int] = []

    async def _select(pool, owner_id, count, action_type="default", **kw):
        asked.append({"count": int(count), "exclude": sorted(kw.get("exclude_ids") or [])})
        # Стенд отдаёт ровно столько свободных, сколько попросили.
        base = 900
        return [{"id": base + i, "session_str": f"s{i}"} for i in range(int(count))]

    async def _join(session, link, **kw):
        joins.append(1)
        return {"ok": True}

    async def _add_member(pool, node_id, account_id, role="member"):
        members_added.append(int(account_id))

    async def _claim(acc_id):
        return True

    async def _release(ids):
        return None

    async def _progress(pool, op_id, done):
        return None

    async def _not_cancelled(pool, op_id):
        return False

    async def _nap(*a, **kw):
        return None

    monkeypatch.setattr(resource_selector, "select_accounts", _select)
    monkeypatch.setattr(account_manager, "join_channel", _join)
    monkeypatch.setattr(nodes_engine, "add_node_member", _add_member)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_mark_progress", _progress)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    monkeypatch.setattr(op_worker, "_governed_sleep", _nap)
    monkeypatch.setattr(asyncio, "sleep", _nap)
    return {"asked": asked, "joins": joins, "added": members_added}


def _liven(pool, count=5):
    from services import op_worker

    return _run(op_worker._exec_community_liven(
        pool, _Bot(), 960, 55, {"node_id": 7, "count": count}))


def test_a_full_node_is_not_joined_again(stand):
    """В ноде уже столько, сколько просили — вступать некому."""
    res = _liven(_Pool(members=[901, 902, 903, 904, 905]), count=5)

    assert stand["joins"] == [], (
        "повтор прогона снова вступил в ноду: до пятидесяти joinChannel в счёт "
        "лимитов на действие, которое уже сделано")
    assert res.get("status") == "done", res
    assert "уже" in (res.get("summary") or ""), (
        f"владелец не прочитает, почему никто не вступал: {res.get('summary')!r}")


def test_a_repeat_tops_up_to_the_asked_count(stand):
    """В ноде трое, просили пятерых — добираем двоих, а не ещё пятерых."""
    res = _liven(_Pool(members=[901, 902, 903]), count=5)

    assert stand["asked"], "выбор аккаунтов не вызывался вовсе"
    assert stand["asked"][0]["count"] == 2, (
        "просим у выбора полный count, а не остаток: владелец просил пять, а "
        f"в ноде окажется восемь — спросили {stand['asked'][0]['count']}")
    assert len(stand["joins"]) == 2, stand["joins"]


def test_accounts_already_in_the_node_are_not_asked_again(stand):
    """Свои же в ноде исключаются из выбора."""
    _liven(_Pool(members=[901, 902, 903]), count=5)

    assert stand["asked"][0]["exclude"] == [901, 902, 903], (
        "выбор может снова взять аккаунт, который в ноде уже состоит: это "
        f"лишний joinChannel по тому же чату — {stand['asked'][0]['exclude']}")


def test_the_registry_no_longer_calls_this_harmless():
    """Реестр: у исполнителя дверь, а не записка «повтор не вредит»."""
    import importlib.util

    path = os.path.join(ROOT, "tests", "test_every_mutating_executor_is_idempotent.py")
    spec = importlib.util.spec_from_file_location("_idem_registry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "_exec_community_liven" not in mod.HARMLESS, (
        "запись «повтор не вредит» осталась, хотя дверь появилась: реестр "
        "начнёт прятать настоящую находку")
    # Ключ реестра — ИМЯ функции-двери, а не имя таблицы: саму
    # `community_node_members` читает и `_exec_community_set_staff`, чтобы
    # знать кого промоутить, и там это не дверь.
    assert any("_node_members_already" in token for token in mod.DOORS), (
        "дверь оживления ноды не названа в реестре дверей — правило её не "
        "увидит")
