"""Anti-detection (#7) — регрессия: boost/profile-исполнители гейтят карантин.

_exec_boost_subscribers/_bot_starts уже отсеивали аккаунты в карантине через
_filter_quarantined_accounts, а _exec_boost_views/_reactions/_stories и
_exec_bulk_set_profile — НЕТ: действие (реакция/просмотр/правка профиля) шло
через флагнутый аккаунт = быстрый бан. Гейт добавлен во все.
"""
from __future__ import annotations

import inspect

import pytest

from services import op_worker


@pytest.mark.parametrize("fn_name", [
    "_exec_boost_views",
    "_exec_boost_reactions",
    "_exec_boost_stories",
    "_exec_bulk_set_profile",
    # уже гейтили — фиксируем, чтобы не регрессировали:
    "_exec_boost_subscribers",
    "_exec_boost_bot_starts",
])
def test_executor_filters_quarantine_before_loop(fn_name):
    fn = getattr(op_worker, fn_name)
    src = inspect.getsource(fn)
    assert "_filter_quarantined_accounts" in src, f"{fn_name} не гейтит карантин (#7)"
    # гейт должен стоять до основного цикла по аккаунтам
    gate = src.index("_filter_quarantined_accounts")
    loop = src.index("for idx, acc in enumerate(accounts")
    assert gate < loop, f"{fn_name}: гейт должен быть ДО цикла по аккаунтам"
