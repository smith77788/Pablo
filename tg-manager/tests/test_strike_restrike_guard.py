"""Strike anti-detection (3A) — регрессия: не бить одну цель повторно подряд.

is_strike_allowed был готовым, но dead code (0 call sites) — guard «не бить в
одно время дважды» не enforced. Повторные удары по цели за короткий интервал —
детекшн-паттерн и износ аккаунтов. Подключён в _exec_strike (настраивается
min_restrike_hours, обход params.force=True). Матч по strike_history.target —
тому же значению, что пишется после удара.
"""
from __future__ import annotations

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_disallowed_when_recent_strike_exists():
    from services.strike_engine import is_strike_allowed

    pool = FakePool(fetchrow={"?column?": 1})  # есть недавняя запись
    assert await is_strike_allowed(pool, "@target", 4) is False


@pytest.mark.asyncio
async def test_allowed_when_no_recent_strike():
    from services.strike_engine import is_strike_allowed

    pool = FakePool(fetchrow=None)  # нет недавних ударов
    assert await is_strike_allowed(pool, "@target", 4) is True


@pytest.mark.asyncio
async def test_query_matches_target_column_not_target_id():
    from services.strike_engine import is_strike_allowed

    pool = FakePool(fetchrow=None)
    await is_strike_allowed(pool, "@target", 4)
    q = [q for kind, q in pool.calls if kind == "fetchrow"][-1]
    assert "target=$1" in q  # прежний баг: target_id (несуществующая колонка)
    assert "strike_history" in q


def test_exec_strike_wired_to_guard():
    """_exec_strike должен реально вызывать is_strike_allowed, иначе guard мёртв."""
    import inspect
    from services import op_worker

    src = inspect.getsource(op_worker._exec_strike)
    assert "is_strike_allowed(" in src, "_exec_strike не проверяет повторный удар"
    assert 'params.get("force")' in src, "нет обхода force для сознательного повтора"
