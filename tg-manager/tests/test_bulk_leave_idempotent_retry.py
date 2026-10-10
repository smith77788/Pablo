"""Повтор массового выхода не выходит оттуда, откуда аккаунт уже вышел.

Разрыв — зеркало того, что давно закрыто в массовом вступлении
(tests/test_bulk_join_idempotent_retry), только в другую сторону и с другой
ценой. Повтор операции перезапускает исполнителя заново с `done_items=0`:
после сетевой ошибки (`_maybe_requeue`), после сброса зависшей сторожем, после
перезапуска контейнера. Исполнитель проходил ВЕСЬ список каналов сначала.

Дело не в лишнем вызове. Дневной лимит выходов считается по тому же журналу
`operation_audit`, что и пропуск:

    SELECT COUNT(*) FROM operation_audit
     WHERE account_id=$1 AND action='leave' AND result='success'
       AND occurred_at > NOW() - INTERVAL '24 hours'

то есть повторные строки выжигали суточный бюджет аккаунта на работе, которая
уже сделана, и следующая операция упиралась в лимит на пустом месте.

Ключ составной: цель отрабатывает КАЖДЫЙ аккаунт операции (цикл
accounts × channels), и закрывать канал целиком из-за одного аккаунта значит
не вывести из него остальные — тот же разбор, что у вступления.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    """Пул с журналом успешных действий; запоминает запросы."""

    def __init__(self, audit_rows=None):
        self.audit_rows = audit_rows or []
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "operation_audit" in query:
            return self.audit_rows
        return []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return 0            # дневной лимит выходов не достигнут

    async def execute(self, query, *args):
        self.queries.append(query)
        return "UPDATE 1"


ACCOUNTS = [{"id": 1, "phone": "+70000000001", "session_str": "s1"},
            {"id": 2, "phone": "+70000000002", "session_str": "s2"}]


def _harness(monkeypatch, attempted):
    """Свести операцию к одному наблюдаемому: по каким парам был вызов leave."""
    from services import account_manager, op_worker, resource_selector

    async def _fake_leave(session_str, channel, _acc=None):
        attempted.append((int(_acc["id"]), str(channel)))
        return {"ok": True}

    async def _select_all_active(pool, owner_id, **kw):
        # Честно уважаем include_ids: иначе тест, задающий один аккаунт,
        # молча проверял бы поведение двух.
        want = kw.get("include_ids")
        return [dict(a) for a in ACCOUNTS
                if not want or int(a["id"]) in {int(x) for x in want}]

    async def _claim(op_id, accounts, owner_id):
        return list(accounts)

    async def _not_quarantined(pool, acc_id):
        return False

    async def _instant(*a, **kw):
        return None

    monkeypatch.setattr(account_manager, "leave_channel", _fake_leave)
    monkeypatch.setattr(resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "_claim_available_accounts", _claim)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    # Пейсинг между выходами — десятки секунд по построению (анти-бан). Здесь
    # важно, КАКИЕ цели берутся, а не как они разнесены: пейсинг проверяют свои
    # тесты.
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return op_worker


@pytest.mark.asyncio
async def test_retry_skips_pairs_this_account_already_left(monkeypatch):
    """Главный регресс."""
    attempted: list[tuple[int, str]] = []
    op_worker = _harness(monkeypatch, attempted)

    # Прошлый прогон успел вывести аккаунт 1 из alpha.
    pool = _Pool(audit_rows=[{"account_id": 1, "target": "alpha"}])
    res = await op_worker._exec_bulk_leave(
        pool, object(), 77, 555,
        {"channels": ["alpha", "beta"], "account_ids": [1, 2], "delay_mode": "fast"},
    )

    assert (1, "alpha") not in attempted, (
        "повтор снова выходил из канала, который аккаунт уже покинул: лишний "
        "вызов и, главное, лишняя строка в суточном бюджете выходов"
    )
    assert sorted(attempted) == [(1, "beta"), (2, "alpha"), (2, "beta")], attempted
    assert res["ok"] == 4, (
        f"уже выполненная пара не засчитана (ok={res['ok']}): владелец увидит "
        f"недобор на полностью доведённой операции"
    )
    assert res["failed"] == 0


@pytest.mark.asyncio
async def test_one_account_done_does_not_close_the_channel_for_others(monkeypatch):
    """Ключ обязан быть парой, а не каналом.

    Если пропускать канал целиком, остальные аккаунты останутся в нём сидеть, а
    операция отчитается, что всё сделано.
    """
    attempted: list[tuple[int, str]] = []
    op_worker = _harness(monkeypatch, attempted)

    pool = _Pool(audit_rows=[{"account_id": 1, "target": "alpha"}])
    await op_worker._exec_bulk_leave(
        pool, object(), 77, 555,
        {"channels": ["alpha"], "account_ids": [1, 2], "delay_mode": "fast"},
    )
    assert attempted == [(2, "alpha")], (
        "успех одного аккаунта закрыл канал для остальных — они остались внутри"
    )


@pytest.mark.asyncio
async def test_first_run_attempts_everything(monkeypatch):
    """Контрольный случай: пустой журнал — пропусков быть не должно."""
    attempted: list[tuple[int, str]] = []
    op_worker = _harness(monkeypatch, attempted)

    pool = _Pool(audit_rows=[])
    await op_worker._exec_bulk_leave(
        pool, object(), 77, 555,
        {"channels": ["alpha", "beta"], "account_ids": [1, 2], "delay_mode": "fast"},
    )
    assert sorted(attempted) == [
        (1, "alpha"), (1, "beta"), (2, "alpha"), (2, "beta")], attempted


@pytest.mark.asyncio
async def test_unreadable_journal_does_not_break_the_operation(monkeypatch):
    """Не прочитали журнал — работаем как раньше: лишний повтор дешевле срыва."""
    attempted: list[tuple[int, str]] = []
    op_worker = _harness(monkeypatch, attempted)

    class _BrokenPool(_Pool):
        async def fetch(self, query, *args):
            if "operation_audit" in query:
                raise RuntimeError("журнал недоступен")
            return await super().fetch(query, *args)

    res = await op_worker._exec_bulk_leave(
        _BrokenPool(), object(), 77, 555,
        {"channels": ["alpha"], "account_ids": [1], "delay_mode": "fast"},
    )
    assert attempted == [(1, "alpha")]
    assert res["ok"] == 1
