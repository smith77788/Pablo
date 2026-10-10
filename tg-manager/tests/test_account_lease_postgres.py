"""Межпроцессный захват аккаунтов по НАСТОЯЩЕМУ Postgres: две реплики не могут
взять одну сессию.

ЗАЧЕМ ОТДЕЛЬНО. Инвариант продукта — «одна auth-key сессия НИКОГДА не коннектится
из двух мест» (иначе Telegram убивает ключ: AUTH_KEY_DUPLICATED, безвозвратно).
Раньше арбитром был set в памяти процесса (`_accounts_in_use`), а флаг
`in_operation` в БД писался вдогонку (fire-and-forget) и авторитетом не был.
Пока процесс один — верно; вторая реплика видит СВОЙ пустой set и берёт те же
аккаунты. Заглушкой пула это невоспроизводимо в принципе: нужна настоящая БД,
где два UPDATE конкурируют за строку.

Две реплики моделируются честно: два НЕЗАВИСИМЫХ экземпляра модуля op_worker
(importlib), у каждого своя память, база одна — ровно как два контейнера.

КАК ЗАПУСТИТЬ (2 минуты, Postgres 16):

    D=/var/tmp/pgtest; mkdir -p $D/pg $D/sock; chown -R postgres $D
    su postgres -s /bin/bash -c "initdb -D $D/pg -U postgres -A trust"
    su postgres -s /bin/bash -c "pg_ctl -D $D/pg \\
        -o \\"-p 55432 -k $D/sock -c listen_addresses=''\\" -l $D/pg/log start"
    psql -h $D/sock -p 55432 -U postgres -c 'CREATE DATABASE infra'
    export INFRAGRAM_TEST_DSN="postgresql://postgres@/infra?host=$D/sock&port=55432"
    pytest tests/test_account_lease_postgres.py -v

Без переменной окружения файл пропускается — обычный прогон и CI не ломаются.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991001

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    """Один цикл на модуль: пул asyncpg привязан к циклу, в котором создан."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _fresh_op_worker(tag: str):
    """Независимый экземпляр op_worker — своя память, как отдельный процесс."""
    path = os.path.join(ROOT, "services", "op_worker.py")
    spec = importlib.util.spec_from_file_location(f"_replica_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _prepare(pool):
    """Минимальная tg_accounts + миграция аренды (её же и проверяем).

    Всё живёт в ОТДЕЛЬНОЙ схеме Postgres: этот файл может делить базу с
    tests/test_invite_e2e_postgres.py, а тот накатывает полную схему в public.
    Работая в public, мы бы дропали его tg_accounts и роняли его тесты.
    """
    await pool.execute("DROP TABLE IF EXISTS tg_accounts")
    await pool.execute(
        """CREATE TABLE tg_accounts (
               id            SERIAL PRIMARY KEY,
               owner_id      BIGINT NOT NULL,
               phone         TEXT,
               session_str   TEXT,
               is_active     BOOLEAN NOT NULL DEFAULT TRUE,
               in_operation  BOOLEAN NOT NULL DEFAULT FALSE
           )"""
    )
    # Ровно та миграция, что уезжает в прод.
    # Комментарии идут ПЕРЕД первым ';', поэтому отбрасывать чанк целиком нельзя
    # (так теряется первый ALTER) — вычищаем построчно.
    sql_path = os.path.join(ROOT, "schema_v180.sql")
    with open(sql_path, encoding="utf-8") as f:
        for chunk in f.read().split(";"):
            body = "\n".join(
                ln for ln in chunk.split("\n") if not ln.strip().startswith("--")
            ).strip()
            if body:
                await pool.execute(body)
    for i in range(1, 7):
        await pool.execute(
            "INSERT INTO tg_accounts(id, owner_id, phone, session_str) "
            "VALUES($1,$2,$3,'sess')", i, OWNER, f"+7999000000{i}")


@pytest.fixture(scope="module")
def stand():
    import asyncpg

    async def _mk():
        # Своя схема-песочница: не пересекаемся с public, где другой postgres-тест
        # держит полную боевую схему.
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS lease_test")
        finally:
            await setup.close()
        pool = await asyncpg.create_pool(
            DSN, min_size=2, max_size=10,
            server_settings={"search_path": "lease_test"},
        )
        await _prepare(pool)
        return pool

    pool = _run(_mk())
    a = _fresh_op_worker("a")
    b = _fresh_op_worker("b")
    a.init_op_worker_pool(pool)
    b.init_op_worker_pool(pool)
    # Разные worker-id: это две разные реплики, а не один процесс дважды.
    assert a._WORKER_ID != b._WORKER_ID, "реплики обязаны иметь разные id аренды"
    yield pool, a, b
    _run(pool.close())


def _reset(pool, *mods):
    _run(pool.execute(
        "UPDATE tg_accounts SET in_operation=FALSE, op_lease_until=NULL, op_lease_owner=NULL"))
    for m in mods:
        m._accounts_in_use.clear()
        m._operation_account_locks.clear()


# ── ядро: две реплики не пересекаются ────────────────────────────────────────

def test_two_replicas_never_claim_the_same_account(stand):
    pool, a, b = stand
    _reset(pool, a, b)
    ids = [1, 2, 3, 4, 5, 6]
    got_a = _run(a.try_claim_accounts(ids))
    got_b = _run(b.try_claim_accounts(ids))
    assert set(got_a) & set(got_b) == set(), (
        f"ОДНА СЕССИЯ В ДВУХ РЕПЛИКАХ → AUTH_KEY_DUPLICATED: "
        f"A={sorted(got_a)} B={sorted(got_b)}")
    assert set(got_a) == set(ids), "первая реплика должна взять всё свободное"
    assert got_b == [], "второй ничего не остаётся"


def test_concurrent_claim_is_disjoint(stand):
    """Одновременный захват (не последовательный) — тоже дизъюнктен."""
    pool, a, b = stand
    _reset(pool, a, b)
    ids = [1, 2, 3, 4, 5, 6]

    async def _both():
        return await asyncio.gather(a.try_claim_accounts(ids), b.try_claim_accounts(ids))

    got_a, got_b = _run(_both())
    assert set(got_a) & set(got_b) == set(), (
        f"гонка двух реплик выдала общий аккаунт: A={sorted(got_a)} B={sorted(got_b)}")
    assert set(got_a) | set(got_b) == set(ids), "вместе должны разобрать весь флот"


def test_operation_claim_is_cross_process_exclusive(stand):
    """_claim_available_accounts (путь операций) — тоже через межпроцессный арбитр."""
    pool, a, b = stand
    _reset(pool, a, b)
    accs = [{"id": i} for i in (1, 2, 3)]
    claimed_a = _run(a._claim_available_accounts(7001, accs))
    claimed_b = _run(b._claim_available_accounts(7002, accs))
    ida = {int(x["id"]) for x in claimed_a}
    idb = {int(x["id"]) for x in claimed_b}
    assert ida & idb == set(), f"операции двух реплик делят аккаунт: {ida & idb}"
    assert ida == {1, 2, 3} and idb == set()


def test_ghost_in_one_replica_blocks_operation_in_other(stand):
    """Фоновая сессия (призрак/прогрев) в реплике A закрывает аккаунт для операции в B."""
    pool, a, b = stand
    _reset(pool, a, b)
    assert _run(a.try_claim_account(4)) is True
    claimed = _run(b._claim_available_accounts(7003, [{"id": 4}, {"id": 5}]))
    ids = {int(x["id"]) for x in claimed}
    assert 4 not in ids, "призрак другой реплики не защитил сессию"
    assert 5 in ids


# ── освобождение и аренда ────────────────────────────────────────────────────

def test_release_only_frees_own_accounts(stand):
    """Реплика НЕ может освободить чужой аккаунт (иначе снимет защиту с живой сессии)."""
    pool, a, b = stand
    _reset(pool, a, b)
    assert _run(a.try_claim_accounts([1, 2])) == [1, 2]
    _run(b.release_accounts([1, 2]))          # чужая реплика пытается освободить
    still = _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE id=ANY('{1,2}') AND in_operation"))
    assert still == 2, "чужая реплика сняла защиту с занятых аккаунтов"
    # владелец освобождает — теперь свободны
    _run(a.release_accounts([1, 2]))
    freed = _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE id=ANY('{1,2}') AND in_operation"))
    assert freed == 0


def test_expired_lease_is_reclaimable(stand):
    """Процесс упал → аренда истекла → аккаунт возвращается в оборот сам."""
    pool, a, b = stand
    _reset(pool, a, b)
    assert _run(a.try_claim_accounts([3])) == [3]
    # эмулируем смерть A: аренда в прошлом, память A недоступна
    _run(pool.execute(
        "UPDATE tg_accounts SET op_lease_until = now() - interval '1 minute' WHERE id=3"))
    assert _run(b.try_claim_accounts([3])) == [3], "истёкшая аренда не переиспользуется"


def test_heartbeat_extends_lease(stand):
    """Живой процесс продлевает аренду — его аккаунты не уводят из-под него."""
    pool, a, b = stand
    _reset(pool, a, b)
    _run(a.try_claim_accounts([5]))
    _run(pool.execute(
        "UPDATE tg_accounts SET op_lease_until = now() + interval '5 seconds' WHERE id=5"))
    _run(a.renew_leases())
    left = _run(pool.fetchval(
        "SELECT EXTRACT(EPOCH FROM (op_lease_until - now())) FROM tg_accounts WHERE id=5"))
    assert left > 60, f"heartbeat не продлил аренду (осталось {left:.0f}с)"
    # и чужая реплика по-прежнему не может забрать
    assert _run(b.try_claim_accounts([5])) == []


def test_startup_reset_keeps_live_lease_of_other_replica(stand):
    """Перезапуск одной реплики НЕ освобождает аккаунты, занятые другой (живой)."""
    pool, a, b = stand
    _reset(pool, a, b)
    _run(a.try_claim_accounts([6]))                 # A держит живую аренду
    _run(b.reset_stale_in_operation(pool))          # B стартует и чистит «залипшее»
    still = _run(pool.fetchval("SELECT in_operation FROM tg_accounts WHERE id=6"))
    assert still is True, "рестарт соседа снял защиту с живой сессии другой реплики"
    # а вот протухшую аренду тот же вызов обязан убрать
    _run(pool.execute(
        "UPDATE tg_accounts SET op_lease_until = now() - interval '1 hour' WHERE id=6"))
    _run(b.reset_stale_in_operation(pool))
    cleared = _run(pool.fetchval("SELECT in_operation FROM tg_accounts WHERE id=6"))
    assert cleared is False, "протухшая аренда должна освобождаться на старте"


def test_db_failure_fails_closed(stand):
    """Нет арбитра — нет захвата. Брать сессию без арбитража опаснее, чем не взять."""
    pool, a, b = stand
    _reset(pool, a, b)

    class _Broken:
        async def fetch(self, *_a, **_k):
            raise RuntimeError("БД недоступна")
        async def execute(self, *_a, **_k):
            raise RuntimeError("БД недоступна")

    saved = a._db_pool
    a._db_pool = _Broken()
    try:
        assert _run(a.try_claim_accounts([1, 2])) == [], "при сбое БД захват должен быть пустым"
        assert a._accounts_in_use == set(), "локальный арбитр обязан откатиться"
    finally:
        a._db_pool = saved
