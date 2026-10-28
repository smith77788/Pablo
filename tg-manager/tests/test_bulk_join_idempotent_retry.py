"""Повтор массового вступления не вступает туда, где аккаунт уже состоит.

ЧТО БЫЛО. Повтор операции — и автоматический (`_maybe_requeue` после сетевой
ошибки или флуда), и после сброса зависшей — перезапускает исполнителя заново с
`done_items=0`, и тот проходит ВЕСЬ список ссылок сначала. Для вступления это
повторный `joinChannel` туда, где аккаунт уже состоит: Telegram считает его в
лимит вступлений и в давление, ведущее к PEER_FLOOD, а дневной счётчик аккаунта
(`_JOIN_DAY_LIMITS`) выгорает на работе, которая уже сделана.

То есть блип сети сам по себе поднимал риск бана — ровно то, от чего защищает
весь пейсинг вокруг. Массовое вступление входит в самый баноопасный класс
операций продукта, и цена ошибки здесь — мёртвый аккаунт, а не задержка.

`_exec_mass_publish` эту защиту имел (`completed_targets`), вступление — нет.
Ключ у него другой: один и тот же канал берут РАЗНЫЕ аккаунты, поэтому единица
работы — пара (аккаунт, ссылка), а не ссылка.
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Pool:
    """Пул с журналом успешных действий и записью всех запросов."""

    def __init__(self, audit_rows=None):
        self.audit_rows = audit_rows or []
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        # Счёт суточного бюджета идёт к той же таблице, но форму строки имеет
        # другую (account_id + n). Отдавать ему журнальные строки нельзя: с
        # недавних пор непрочитанный бюджет означает «аккаунты исчерпали
        # лимит», и заглушка, подсунувшая не ту форму, оставила бы исполнителя
        # вообще без аккаунтов — тест падал бы на здоровом коде
        # (tests/test_an_unreadable_budget_does_not_hand_out_the_fleet.py).
        # Отличаем по GROUP BY: он есть только у счёта бюджета. Пробовать на
        # слово COUNT нельзя — его содержит ACCOUNT_ID из журнального запроса,
        # и журнал перестал бы читаться вообще (проверено: тесты краснели).
        if "operation_audit" in query and "GROUP BY" not in query.upper():
            return self.audit_rows
        return []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return 0

    async def execute(self, query, *args):
        self.queries.append(query)
        return "UPDATE 1"


def _no_pacing(monkeypatch):
    """Пейсинг между вступлениями — десятки секунд по построению (анти-бан).

    В тесте нас интересует, КАКИЕ цели берутся, а не как они разнесены во
    времени: сам пейсинг проверяют свои тесты. Убираем только сон.
    """
    import asyncio

    from services import op_worker

    async def _instant(*a, **kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    monkeypatch.setattr(op_worker, "_governed_delay", lambda pool, owner, base: _zero())


async def _zero():
    return 0.0


@pytest.mark.asyncio
async def test_completed_pairs_are_read_per_account_not_per_target():
    from services import op_worker

    pool = _Pool(audit_rows=[
        {"account_id": 1, "target": "t.me/alpha"},
        {"account_id": 2, "target": "t.me/beta"},
    ])
    done = await op_worker.completed_account_targets(pool, 77, "join")
    assert done == {(1, "t.me/alpha"), (2, "t.me/beta")}, (
        "ключ обязан быть составным: один канал берут разные аккаунты, и "
        "пропуск ссылки целиком недоделал бы работу остальных"
    )


@pytest.mark.asyncio
async def test_unreadable_journal_postpones_the_run_instead_of_repeating_it():
    """Не прочитали журнал — уходим на повтор, а не вступаем во всё заново.

    Прежний контракт был обратным: пустое множество на сбое, то есть «ни один
    канал ещё не взят». Для возобновлённого прогона это повторный joinChannel
    по всему списку — лишний расход лимита и прямое давление PEER_FLOOD, то
    есть ровно та цена, от которой дверь и защищает (см. соседний тест про
    пропуск уже взятых каналов). Отказ дороже не стоит: исключение
    классифицируется как 'retry', операция возвращается в очередь и
    доработает позже.
    """
    from services import op_worker
    from services.op_errors import _classify_op_error

    class _Broken(_Pool):
        async def fetch(self, query, *args):
            raise RuntimeError("БД недоступна")

    with pytest.raises(op_worker.JournalUnreadable) as e:
        await op_worker.completed_account_targets(_Broken(), 77, "join")
    assert _classify_op_error(e.value) == "retry", (
        "отказ читать журнал обязан вести к повтору, а не к провалу операции")


@pytest.mark.asyncio
async def test_retry_skips_links_this_account_already_joined(monkeypatch):
    from services import account_manager
    from services import op_worker

    attempted: list[tuple[int, str]] = []

    async def _fake_join(session_str, link, _acc=None):
        attempted.append((int(_acc["id"]), link))
        return {"ok": True}

    async def _not_quarantined(pool, acc_id):
        return False

    monkeypatch.setattr(account_manager, "join_channel", _fake_join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)

    pool = _Pool(audit_rows=[{"account_id": 1, "target": "t.me/alpha"}])
    accounts = [{"id": 1, "phone": "+70000000001", "session_str": "s1"}]
    params = {"links": ["t.me/alpha", "t.me/beta"], "delay_mode": "fast"}

    res = await op_worker._exec_bulk_join_inner(
        pool, object(), 77, 555, params, accounts
    )

    assert attempted == [(1, "t.me/beta")], (
        "повтор снова вступал в канал, где аккаунт уже состоит — это лишний "
        "joinChannel в лимит и в давление PEER_FLOOD"
    )
    assert res["ok"] == 2, (
        f"уже выполненная цель не засчитана (ok={res['ok']}): владелец увидит "
        f"недобор на полностью доведённой операции"
    )
    assert res["failed"] == 0


@pytest.mark.asyncio
async def test_first_run_attempts_everything(monkeypatch):
    """Контрольный случай: без журнала пропусков быть не должно."""
    from services import account_manager
    from services import op_worker

    attempted: list[tuple[int, str]] = []

    async def _fake_join(session_str, link, _acc=None):
        attempted.append((int(_acc["id"]), link))
        return {"ok": True}

    async def _not_quarantined(pool, acc_id):
        return False

    monkeypatch.setattr(account_manager, "join_channel", _fake_join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)

    pool = _Pool(audit_rows=[])
    res = await op_worker._exec_bulk_join_inner(
        pool, object(), 77, 555,
        {"links": ["t.me/alpha", "t.me/beta"], "delay_mode": "fast"},
        [{"id": 1, "phone": "+70000000001", "session_str": "s1"}],
    )
    assert attempted == [(1, "t.me/alpha"), (1, "t.me/beta")]
    assert res["ok"] == 2
