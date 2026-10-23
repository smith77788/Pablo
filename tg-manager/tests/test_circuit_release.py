"""Circuit Breaker (1B) — регрессия: пропущенная операция возвращается в очередь.

Поллер ставит операции status='running' ДО запуска задачи. Если цепь открыта,
задача выходила, оставляя операцию 'running' до stale-watchdog (60 мин) — она
держала фантомный слот параллельности владельца и блокировала его другие
операции, хотя cooldown цепи всего 30 мин. Фикс возвращает её в 'pending' с
отложенным (на cooldown) повтором.
"""
from __future__ import annotations

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_release_returns_op_to_pending_deferred():
    from services import op_worker

    pool = FakePool()
    await op_worker._release_op_for_circuit(pool, op_id=42, cooldown_s=1800)

    execs = [q for kind, q in pool.calls if kind == "execute"]
    assert execs, "операция не была возвращена в очередь"
    q = execs[-1]
    # возвращаем в pending (иначе не переотберётся) и снимаем started_at
    assert "status='pending'" in q
    assert "started_at=NULL" in q
    # откладываем повтор через scheduled_for (иначе busy-loop при открытой цепи)
    assert "scheduled_for" in q


@pytest.mark.asyncio
async def test_release_handles_zero_cooldown():
    from services import op_worker

    pool = FakePool()
    # даже при нулевом/отрицательном cooldown не падаем и всё равно откладываем
    await op_worker._release_op_for_circuit(pool, op_id=7, cooldown_s=0)
    assert any(kind == "execute" for kind, _ in pool.calls)
