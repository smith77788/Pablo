"""Безопасность сессий: одна auth-key сессия НИКОГДА не коннектится из двух мест.

Раньше фоновые циклы (призрак/прогрев/пре-флайт) выбирали аккаунты по снимку
`in_operation=FALSE` в БД, но не захватывали их в in-memory арбитре op_worker —
операция могла параллельно открыть ту же сессию → Telegram видит вход с двух
мест и УНИЧТОЖАЕТ auth-key (AUTH_KEY_DUPLICATED, безвозвратно).

Фикс: атомарный захват try_claim_account(s) под единым `_accounts_lock` —
общий арбитр `_accounts_in_use` для операций И фоновых сессий.
"""
from __future__ import annotations

import asyncio
import os

import pytest

import services.op_worker as ow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate():
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    saved_locks = dict(ow._operation_account_locks)
    ow._db_pool = None  # без БД — чистая in-memory проверка арбитра
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    try:
        yield
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)
        ow._operation_account_locks.clear()
        ow._operation_account_locks.update(saved_locks)


def test_try_claim_account_true_when_free_false_when_held():
    assert asyncio.run(ow.try_claim_account(42)) is True   # свободен → захвачен
    assert 42 in ow._accounts_in_use
    assert asyncio.run(ow.try_claim_account(42)) is False  # уже держим → отказ


def test_try_claim_accounts_returns_only_free_subset():
    ow._accounts_in_use.update({2, 4})  # заняты
    claimed = asyncio.run(ow.try_claim_accounts([1, 2, 3, 4, 5]))
    assert set(claimed) == {1, 3, 5}                 # только свободные
    assert ow._accounts_in_use == {1, 2, 3, 4, 5}    # все теперь заняты


def test_ghost_claim_blocks_operation_claim_same_account():
    # призрак захватил аккаунт 7 …
    assert asyncio.run(ow.try_claim_account(7)) is True
    # … операция НЕ должна получить его в свою долю
    claimed = asyncio.run(ow._claim_available_accounts(900, [{"id": 7}, {"id": 8}]))
    ids = [a["id"] for a in claimed]
    assert 7 not in ids and 8 in ids


def test_operation_claim_blocks_ghost_claim_same_account():
    # операция захватила аккаунт 9 …
    asyncio.run(ow._claim_available_accounts(901, [{"id": 9}]))
    # … призрак/прогрев не может открыть на нём вторую сессию
    assert asyncio.run(ow.try_claim_account(9)) is False


def test_release_returns_account_to_arbiter():
    asyncio.run(ow.try_claim_account(11))
    asyncio.run(ow.release_accounts([11]))
    assert 11 not in ow._accounts_in_use
    assert asyncio.run(ow.try_claim_account(11)) is True  # снова захватываем


# ─── Wiring: живые сессии проходят через арбитр ────────────────────────────────

def test_ghost_engine_claims_and_releases():
    src = open(os.path.join(ROOT, "services", "ghost_engine.py"), encoding="utf-8").read()
    assert "try_claim_account" in src
    assert "release_accounts([account_id])" in src
    # захват ДО открытия клиента
    i_claim = src.index("try_claim_account")
    i_client = src.index("_make_client(session")
    assert i_claim < i_client, "захват должен предшествовать открытию сессии"
    # освобождение в finally
    assert "finally:" in src[i_client:]


def test_warmer_uses_atomic_claim_not_unconditional_mark():
    src = open(os.path.join(ROOT, "services", "account_warmer.py"), encoding="utf-8").read()
    # оба пути прогрева (одиночный план + мультисессия) — через атомарный захват
    assert src.count("try_claim_account") >= 2
    # больше не безусловный mark_accounts_in_use (он допускал двойную сессию)
    assert "await _opw.mark_accounts_in_use([account_id])" not in src


def test_invite_preflight_uses_atomic_batch_claim():
    src = open(os.path.join(ROOT, "services", "invite_preflight.py"), encoding="utf-8").read()
    assert "try_claim_accounts" in src
    # старый небезопасный check-then-mark убран
    assert "if _opw.is_account_in_use(int(a[\"id\"]))" not in src


def test_strike_claims_atomically_and_works_only_on_claimed():
    src = open(os.path.join(ROOT, "services", "strike_engine.py"), encoding="utf-8").read()
    i = src.index("async def staggered_strike")
    body = src[i:i + 3000]
    assert "try_claim_accounts" in body
    # больше не безусловный mark всех аккаунтов плана
    assert "await _opw.mark_accounts_in_use(_claimed_ids)" not in body
    # страйк работает только с реально захваченными сессиями
    assert "plan.accounts = [a for a in plan.accounts" in body
