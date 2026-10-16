"""Накрутка уважает суточный лимит действий аккаунта и учитывается в бюджете.

Аудит F13: boost_subscribers/reactions/views выбирали аккаунты без суточного
лимита и НЕ писали свои действия в operation_audit. Значит один аккаунт мог за
сутки вступить в десятки каналов / поставить десятки реакций (сверхчеловеческая
активность = сигнатура фермы), а бюджетный учёт этого не видел.

Фикс: перед действием бусты отсеивают аккаунты, исчерпавшие суточный лимит
(_filter_over_budget_accounts → account_budget.filter_within_budget), и на каждое
успешное действие пишут _audit (join/view/reaction), чтобы лимит их учитывал.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import op_worker

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _body(fn: str) -> str:
    i = WORKER.index(f"async def {fn}(")
    j = WORKER.index("\nasync def ", i + 1)
    return WORKER[i:j]


def _run(coro):
    return asyncio.run(coro)


# ── helper фильтрует по бюджету ───────────────────────────────────────────────

def test_filter_over_budget_drops_exhausted_accounts():
    accs = [{"id": 1}, {"id": 2}, {"id": 3}]
    with patch("services.account_budget.filter_within_budget",
               AsyncMock(return_value=([1, 3], [2]))):
        kept, skipped = _run(op_worker._filter_over_budget_accounts(None, 5, accs))
    assert {a["id"] for a in kept} == {1, 3}
    assert skipped == 1


def test_filter_over_budget_fail_open_on_error():
    accs = [{"id": 1}, {"id": 2}]
    with patch("services.account_budget.filter_within_budget",
               AsyncMock(side_effect=Exception("db"))):
        kept, skipped = _run(op_worker._filter_over_budget_accounts(None, 5, accs))
    assert kept == accs and skipped == 0


# ── все три буста применяют лимит и пишут audit ───────────────────────────────

def test_all_boosts_apply_budget_and_audit():
    for fn, action in (("_exec_boost_subscribers", "join"),
                       ("_exec_boost_reactions", "reaction"),
                       ("_exec_boost_views", "view")):
        body = _body(fn)
        assert "_filter_over_budget_accounts(pool, op_id, accounts)" in body, (
            f"{fn} обязан отсеивать исчерпавших суточный лимит")
        assert f'_audit(pool, owner_id, "{action}", "ok"' in body, (
            f"{fn} обязан писать {action} в operation_audit на успех")
        assert "исчерпали суточный лимит" in body, (
            f"{fn} обязан честно отложить операцию, если лимит исчерпан")


# ── F10: свежие непрогретые аккаунты не идут в накрутку ───────────────────────

class _FreshPool:
    def __init__(self, fresh_ids):
        self._fresh = fresh_ids

    async def fetch(self, q, *a):
        return [{"id": i} for i in self._fresh]


def test_unwarmed_fresh_accounts_are_dropped(monkeypatch):
    monkeypatch.setenv("BOOST_MIN_ACCOUNT_AGE_HOURS", "1")
    accs = [{"id": 1}, {"id": 2}, {"id": 3}]
    kept, skipped = _run(op_worker._filter_unwarmed_fresh_accounts(
        _FreshPool([2]), 5, accs))
    assert {a["id"] for a in kept} == {1, 3}
    assert skipped == 1


def test_fresh_filter_disabled_by_zero(monkeypatch):
    monkeypatch.setenv("BOOST_MIN_ACCOUNT_AGE_HOURS", "0")
    accs = [{"id": 1}, {"id": 2}]
    kept, skipped = _run(op_worker._filter_unwarmed_fresh_accounts(
        _FreshPool([1, 2]), 5, accs))
    assert kept == accs and skipped == 0


def test_fresh_filter_fail_open(monkeypatch):
    monkeypatch.setenv("BOOST_MIN_ACCOUNT_AGE_HOURS", "1")

    class _Boom:
        async def fetch(self, q, *a):
            raise Exception("db")

    accs = [{"id": 1}]
    kept, skipped = _run(op_worker._filter_unwarmed_fresh_accounts(_Boom(), 5, accs))
    assert kept == accs and skipped == 0


def test_all_boosts_apply_fresh_filter():
    for fn in ("_exec_boost_subscribers", "_exec_boost_reactions", "_exec_boost_views"):
        assert "_filter_unwarmed_fresh_accounts(pool, op_id, accounts)" in _body(fn)
