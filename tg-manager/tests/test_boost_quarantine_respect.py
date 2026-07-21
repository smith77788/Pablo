"""Anti-detection: boost-исполнители по реальным аккаунтам уважают риск-пульс.

Boost-действия — тоже массовые операции по реальным аккаунтам. Action-verb boost'ы
(подписка = join, /start = send) флагуют аккаунт при лимитах/координации; аккаунт под
недавним серьёзным ограничением при этом идёт в бан. Общий гейт
`_filter_quarantined_accounts` (fail-open) + честный показ пропуска в итоге.
"""
from __future__ import annotations

import asyncio
import inspect

from services import op_worker


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_helper_fail_open_and_counts(monkeypatch):
    accounts = [{"id": 1}, {"id": 2}, {"id": 3}]

    async def _quar(pool, aid):
        return aid == 2
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quar)

    kept, skipped = _run(op_worker._filter_quarantined_accounts(None, 1, accounts))
    assert skipped == 1 and [a["id"] for a in kept] == [1, 3]


def test_helper_fail_open_when_all_quarantined(monkeypatch):
    accounts = [{"id": 1}, {"id": 2}]

    async def _all(pool, aid):
        return True
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _all)

    kept, skipped = _run(op_worker._filter_quarantined_accounts(None, 1, accounts))
    # все в карантине → НЕ обнуляем (лучше рискнуть, чем сорвать операцию)
    assert skipped == 0 and len(kept) == 2


def test_helper_fail_open_on_error(monkeypatch):
    accounts = [{"id": 1}, {"id": 2}]

    async def _boom(pool, aid):
        raise RuntimeError("db down")
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _boom)

    kept, skipped = _run(op_worker._filter_quarantined_accounts(None, 1, accounts))
    assert skipped == 0 and len(kept) == 2, "ошибка проверки не должна блокировать ядро"


def test_boost_action_verbs_use_quarantine_gate():
    # подписчики (join) и старты (/start) — обязаны звать общий гейт
    for fn in (op_worker._exec_boost_subscribers, op_worker._exec_boost_bot_starts):
        src = inspect.getsource(fn)
        assert "_filter_quarantined_accounts" in src, f"{fn.__name__} без quarantine-гейта"
        assert "риск-пульс" in src, f"{fn.__name__} должен честно показывать пропуск"
