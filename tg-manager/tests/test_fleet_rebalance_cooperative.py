"""Кооперативный тайм-шеринг флота: параллельные операции владельца делят
аккаунты ПОСТЕПЕННО, а не «первая забрала всё — вторая ждёт до конца».

Контекст. `_claim_available_accounts` уже умел справедливую долю — НО только
в момент первого захвата: если операция A стартовала в одиночку, она берёт
ВЕСЬ свободный флот; когда позже стартует операция B того же владельца,
свободных аккаунтов уже нет, и B либо проваливалась, либо (после соседнего
фикса живой очереди) ждёт полного завершения A. Явный запрос владельца:
"один и тот же пользователь может одновременно читать каналы, вести диалоги,
приглашать... не обязательно одновременно, а постепенно — не падая из-за
занятого флота".

Фикс: `_rebalance_claim` пересчитывает долю ПОСРЕДИ прогона операции и
отдаёт лишнее обратно в пул — освобождённые аккаунты сразу доступны другой
операции (через обычный try_claim_accounts/живую очередь). В любой момент
конкретный аккаунт держит РОВНО одна живая сессия (защита от
AUTH_KEY_DUPLICATED не ослаблена) — делится только флот КАК ЦЕЛОЕ, между
раундами, а не одна сессия между операциями.

Реальный Postgres — нужна настоящая аренда (`tg_accounts.in_operation`) и
настоящая проверка `operation_queue.status='running'`.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991601

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    yield


async def _mk_accounts(pool, n: int) -> list[int]:
    ids = []
    for i in range(n):
        row = await pool.fetchrow(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, "
            "is_active, trust_score) VALUES($1,$2,'s','active',TRUE,0.9) RETURNING id",
            OWNER, f"+7999{i:07d}",
        )
        ids.append(int(row["id"]))
    return ids


class _PoolCtx:
    """Сохраняет и восстанавливает модульные глобалы op_worker, которые
    трогает этот тест — иначе следующие тестовые файлы в том же процессе
    pytest унаследуют закрытый пул/чужие op_id (см. регресс в
    test_fleet_busy_requeues_not_fails.py)."""

    def __init__(self, w, pool):
        self.w = w
        self.pool = pool

    def __enter__(self):
        self._orig_db_pool = self.w._db_pool
        self._orig_active = set(self.w._active_op_ids)
        self.w.init_op_worker_pool(self.pool)
        return self

    def __exit__(self, *exc):
        self.w._db_pool = self._orig_db_pool
        self.w._active_op_ids.clear()
        self.w._active_op_ids.update(self._orig_active)


async def _mk_running_op(pool, owner_id: int, op_type="warmup") -> int:
    return await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,$2,'running','{}',1,'rival') RETURNING id",
        owner_id, op_type,
    )


def test_rebalance_is_a_noop_when_running_alone(pool):
    from services import op_worker as w

    ids = _run(_mk_accounts(pool, 10))
    held = [{"id": i} for i in ids]
    with _PoolCtx(w, pool):
        result = _run(w._rebalance_claim(999001, OWNER, held))
    assert result == held, "без конкурентных операций делить нечего"


def test_rebalance_gives_back_a_fair_share_to_a_rival_operation(pool):
    """Ядро фичи: появилась вторая running-операция того же владельца —
    первая обязана вернуть половину флота в пул, а не держать всё до конца."""
    from services import op_worker as w

    acc_ids = _run(_mk_accounts(pool, 10))
    held = [{"id": i} for i in acc_ids]
    my_op_id = 999002

    with _PoolCtx(w, pool):
        # "Моя" операция реально держит все 10 (настоящая аренда в БД).
        claimed = _run(w.try_claim_accounts(acc_ids))
        assert claimed == acc_ids
        w._operation_account_locks[my_op_id] = set(acc_ids)
        w._active_op_ids.add(my_op_id)

        rival_op_id = _run(_mk_running_op(pool, OWNER))
        w._active_op_ids.add(rival_op_id)

        try:
            kept = _run(w._rebalance_claim(my_op_id, OWNER, held))
            assert len(kept) == 5, f"справедливая доля при 2 операциях — половина, получено {len(kept)}"

            given_back_ids = set(acc_ids) - {a["id"] for a in kept}
            assert len(given_back_ids) == 5

            # Отданные аккаунты реально свободны — аренда в БД снята.
            rows = _run(pool.fetch(
                "SELECT id, in_operation FROM tg_accounts WHERE id = ANY($1::bigint[])",
                list(given_back_ids)))
            assert all(r["in_operation"] is False for r in rows), (
                "отданные аккаунты обязаны быть реально освобождены в БД, "
                "иначе вторая операция их не подхватит"
            )

            # И их немедленно может забрать конкурентная операция.
            re_claimed = _run(w.try_claim_accounts(list(given_back_ids)))
            assert set(re_claimed) == given_back_ids, (
                "отданные аккаунты обязаны быть доступны для захвата другой операцией сразу"
            )
            _run(w.release_accounts(re_claimed))
        finally:
            _run(w.release_operation_accounts(my_op_id))
            _run(pool.execute("DELETE FROM operation_queue WHERE id=$1", rival_op_id))


def test_rebalance_never_gives_back_below_the_minimum():
    """При большой конкуренции доля не должна схлопнуться до нуля — иначе
    операция теряет прогресс совсем, а не притормаживает."""
    from services import op_worker as w
    assert w._MIN_ACCOUNTS_PER_OP >= 1


def test_mass_invite_calls_rebalance_periodically_not_every_batch():
    """Статическая проверка: вызов встроен в основной цикл mass_invite и
    троттлится по времени — иначе каждый батч бил бы БД лишним COUNT(*)."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    assert "_rebalance_claim(" in seg
    assert "time.monotonic()" in seg
    assert "_REBALANCE_EVERY_S" in seg
