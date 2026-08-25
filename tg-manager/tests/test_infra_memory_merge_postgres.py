"""infra_memory: счётчики СЛИВАЮТСЯ, а не затираются (находка аудита №3).

Флаш писал абсолютные значения: `successes = EXCLUDED.successes`. В том же
запросе метки времени сливались через GREATEST — счётчики были единственным
местом без слияния. Следствия:
  • две реплики затирают инкременты друг друга (данные теряются молча);
  • если бы флаш просто прибавлял абсолют, загруженное из БД прибавлялось бы
    поверх себя же и счётчики УДВАИВАЛИСЬ бы на каждом рестарте.

Обе ошибки видны только на настоящей БД: заглушка пула не выполняет SQL и
ON CONFLICT в ней не срабатывает вовсе. Запуск — INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


ACC = 987654


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await p.execute(
            """CREATE TABLE IF NOT EXISTS infra_memory_accounts (
                   account_id      BIGINT NOT NULL,
                   action_type     TEXT   NOT NULL,
                   successes       INT    NOT NULL DEFAULT 0,
                   failures        INT    NOT NULL DEFAULT 0,
                   last_success_at TIMESTAMPTZ,
                   last_failure_at TIMESTAMPTZ,
                   last_errors     TEXT[],
                   avg_duration_s  DOUBLE PRECISION DEFAULT 0,
                   updated_at      TIMESTAMPTZ DEFAULT now(),
                   PRIMARY KEY (account_id, action_type))""")
        return p

    p = _run(_mk())
    yield p
    _run(p.execute("DELETE FROM infra_memory_accounts WHERE account_id=$1", ACC))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    from services import infra_memory as im
    _run(pool.execute("DELETE FROM infra_memory_accounts WHERE account_id=$1", ACC))
    im._account_memory.clear()
    im._dirty_account_keys.clear()
    yield
    im._account_memory.clear()
    im._dirty_account_keys.clear()


def _db_counts(pool):
    row = _run(pool.fetchrow(
        "SELECT successes, failures FROM infra_memory_accounts "
        "WHERE account_id=$1 AND action_type='invite'", ACC))
    return (row["successes"], row["failures"]) if row else (0, 0)


def test_two_replicas_do_not_overwrite_each_other(pool):
    """Главное: инкременты соседа не теряются.

    Реплики моделируются как два независимых состояния модуля, стартовавших с
    одной и той же загруженной из БД базы.
    """
    from services import infra_memory as im

    # общий старт: в БД уже 100 успехов
    _run(pool.execute(
        "INSERT INTO infra_memory_accounts(account_id, action_type, successes, failures) "
        "VALUES($1,'invite',100,0)", ACC))

    def _replica(extra_successes: int):
        im._account_memory.clear(); im._dirty_account_keys.clear()
        im._account_memory[(ACC, "invite")] = im._AccountActionRecord(
            account_id=ACC, action_type="invite", successes=100, failures=0,
            flushed_successes=100, flushed_failures=0)      # как после load_from_db
        for _ in range(extra_successes):
            im.record_account_op(ACC, "invite", success=True)
        _run(im.flush_to_db(pool))

    _replica(5)      # реплика A записала 5 успехов
    _replica(3)      # реплика B записала 3 успеха

    got, _ = _db_counts(pool)
    assert got == 108, (
        f"инкременты потеряны: ожидал 100+5+3=108, в БД {got}. "
        "Абсолютная перезапись затирает работу соседней реплики")


def test_reload_then_flush_does_not_double_counters(pool):
    """Рестарт не должен раздувать счётчики.

    Если флаш прибавляет прирост, но загруженное из БД не помечено как уже
    записанное, первый же флаш прибавит весь абсолют поверх себя.
    """
    from services import infra_memory as im
    _run(pool.execute(
        "INSERT INTO infra_memory_accounts(account_id, action_type, successes, failures) "
        "VALUES($1,'invite',50,7)", ACC))

    _run(im.load_all_from_db(pool))
    rec = im._account_memory.get((ACC, "invite"))
    assert rec is not None, "запись не загрузилась"
    assert rec.flushed_successes == 50 and rec.flushed_failures == 7, (
        "загруженное из БД не помечено записанным — счётчики удвоятся")

    im.record_account_op(ACC, "invite", success=True)     # +1
    _run(im.flush_to_db(pool))
    assert _db_counts(pool) == (51, 7), (
        f"счётчики раздулись при рестарте: {_db_counts(pool)} вместо (51, 7)")


def test_repeated_flush_without_new_events_changes_nothing(pool):
    """Повторный флаш без новых событий не должен ничего прибавлять."""
    from services import infra_memory as im
    im._account_memory[(ACC, "invite")] = im._AccountActionRecord(
        account_id=ACC, action_type="invite")
    im.record_account_op(ACC, "invite", success=True)
    _run(im.flush_to_db(pool))
    first = _db_counts(pool)

    im._dirty_account_keys.add((ACC, "invite"))      # форсируем повтор
    _run(im.flush_to_db(pool))
    assert _db_counts(pool) == first, (
        f"повторный флаш прибавил лишнее: {_db_counts(pool)} после {first}")


def test_failed_flush_keeps_delta_for_retry():
    """Сбой записи не должен «съедать» прирост: отметку двигаем только после успеха."""
    from services import infra_memory as im

    class _Broken:
        async def execute(self, *_a, **_k):
            raise RuntimeError("БД недоступна")

    im._account_memory[(ACC, "invite")] = im._AccountActionRecord(
        account_id=ACC, action_type="invite")
    im.record_account_op(ACC, "invite", success=True)
    _run(im.flush_to_db(_Broken()))
    rec = im._account_memory[(ACC, "invite")]
    assert rec.flushed_successes == 0, "прирост помечен записанным, хотя запись упала"
    assert (ACC, "invite") in im._dirty_account_keys, "ключ не вернулся в очередь повтора"
