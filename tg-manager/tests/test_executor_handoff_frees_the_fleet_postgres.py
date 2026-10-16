"""Передача аренды исполнителя освобождает флот сразу, а не через TTL.

ЗАЧЕМ ЦЕЛИКОМ, А НЕ ПО ЧАСТЯМ. Аренда исполнителя и аренда аккаунтов — две
разные аренды с разными сроками (90 секунд против 15 минут), и опасен именно
их стык. Процесс, у которого забрали аренду исполнителя, перестаёт продлевать
и аренды аккаунтов: продление стоит в цикле ПОСЛЕ гейта. Если он при этом
просто замолчит, его аккаунты останутся занятыми до своего TTL — новый
держатель возьмёт операции, упрётся в «флот занят», уйдёт в отложенный повтор,
а через _ACCT_WAIT_MAX_MIN объявит операцию не дождавшейся флота. То есть
передача аренды, задуманная как защита, сама останавливала бы работу продукта
на четверть часа.

Поэтому сдача аренды (`op_worker._stand_down_after_lease_loss`) снимает свои
аренды аккаунтов немедленно. Здесь это проверяется на РЕАЛЬНОМ SQL обеих
аренд: заглушка пула не исполняет ни предикат захвата (`op_lease_owner = $2 OR
op_lease_until < now()`), ни предикат снятия, то есть «аккаунт снова доступен
другому процессу» на ней не проверяется в принципе.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
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

OWNER = 991779
ACC_IDS = (9917791, 9917792)
_LOOP: "asyncio.AbstractEventLoop | None" = None

# Предикат захвата аренды аккаунта — копия из op_worker._db_claim. Нужен, чтобы
# спросить базу «а ДРУГОЙ процесс эти аккаунты взять сможет?» без второго
# воркера в тесте.
_CLAIM_SQL = """UPDATE tg_accounts
                   SET in_operation = TRUE, op_lease_owner = $2,
                       op_lease_until = now() + make_interval(secs => 900)
                 WHERE id = ANY($1::int[])
                   AND (in_operation = FALSE
                        OR op_lease_until IS NULL
                        OR op_lease_until < now()
                        OR op_lease_owner = $2)
              RETURNING id"""


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture
def worker(pool):
    """op_worker с подключённым пулом: аренды аккаунтов идут через _db_pool."""
    from services import op_worker

    prev = op_worker._db_pool
    op_worker._db_pool = pool
    yield op_worker
    op_worker._db_pool = prev
    op_worker._accounts_in_use.clear()
    op_worker._active_op_ids.clear()
    op_worker._active_op_tasks.clear()


@pytest.fixture(autouse=True)
def _clean(pool):
    async def _prep():
        await pool.execute(
            "DELETE FROM tg_accounts WHERE id = ANY($1::int[])", list(ACC_IDS))
        for i in ACC_IDS:
            await pool.execute(
                "INSERT INTO tg_accounts(id, owner_id, phone, session_str) "
                "VALUES($1,$2,$3,'sess')", i, OWNER, f"+7999{i}")
        await pool.execute("DELETE FROM executor_lease WHERE id = 1")
    _run(_prep())
    yield
    _run(pool.execute(
        "DELETE FROM tg_accounts WHERE id = ANY($1::int[])", list(ACC_IDS)))


async def _free_for(pool, who: str) -> int:
    """Сколько из наших аккаунтов смог бы взять процесс с именем `who`."""
    rows = await pool.fetch(_CLAIM_SQL, list(ACC_IDS), who)
    ids = [int(r["id"]) for r in rows]
    if ids:  # не оставляем за собой чужую аренду
        await pool.execute(
            "UPDATE tg_accounts SET in_operation=FALSE, op_lease_owner=NULL, "
            "op_lease_until=NULL WHERE id = ANY($1::int[])", ids)
    return len(ids)


# ── Главное: после передачи аренды флот свободен ─────────────────────────────

def test_handoff_frees_the_fleet_at_once(worker, pool):
    """Иначе новый держатель четверть часа упирается в «флот занят»."""
    claimed = _run(worker._db_claim(list(ACC_IDS)))
    assert sorted(claimed) == sorted(ACC_IDS), "подготовка: аренда не взята"
    worker._accounts_in_use.update(ACC_IDS)
    worker._active_op_ids.add(1)

    _run(worker._stand_down_after_lease_loss(pool))

    assert _run(_free_for(pool, "новый-держатель")) == len(ACC_IDS), (
        "после сдачи аренды исполнителя аккаунты всё ещё заняты — новый "
        "держатель получит «флот занят» и уведёт операцию в отложенный повтор")
    row = _run(pool.fetchrow(
        "SELECT bool_or(in_operation) AS busy, count(op_lease_owner) AS owned "
        "  FROM tg_accounts WHERE id = ANY($1::int[])", list(ACC_IDS)))
    assert row["busy"] is False and row["owned"] == 0, (
        f"аренда снята не до конца: {dict(row)}")


def test_without_the_handoff_the_fleet_stays_locked(worker, pool):
    """Проверка измерителя: тест выше обязан падать, если сдача ничего не делает."""
    _run(worker._db_claim(list(ACC_IDS)))
    worker._accounts_in_use.update(ACC_IDS)

    assert _run(_free_for(pool, "новый-держатель")) == 0, (
        "чужую живую аренду аккаунта смог забрать другой процесс — это "
        "AUTH_KEY_DUPLICATED, и проверка выше ничего не измеряет")


def test_the_lease_really_changes_hands(worker, pool):
    """Триггер сдачи реалистичен: аренда уходит, только когда срок истёк."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "процесс-A", "worker")) is True
    assert _run(rg.acquire_executor_lease(pool, "процесс-B", "worker")) is False, (
        "второй процесс забрал живую аренду исполнителя")

    _run(pool.execute(
        "UPDATE executor_lease SET expires_at = now() - interval '1 s' WHERE id=1"))
    assert _run(rg.acquire_executor_lease(pool, "процесс-B", "worker")) is True, (
        "просроченная аренда не переходит — умерший держатель запирал бы очередь")
    assert _run(rg.acquire_executor_lease(pool, "процесс-A", "worker")) is False, (
        "прежний держатель не узнаёт, что аренды у него больше нет — значит "
        "и сдачу он не выполнит")
