"""Очередь операций разбирает ОДИН процесс — проверено на живом Postgres.

РАЗРЫВ. Лимиты флуда, потолки параллельности на владельца и троттлы
восстановления живут в ПАМЯТИ процесса. Две реплики с ролью worker считают их
независимо: суммарный темп по флоту выходит за безопасный, а это прямой риск
бана аккаунтов — самая дорогая поломка продукта. Для такого запуска достаточно
одной опечатки в переменных окружения Railway, и до сих пор это только писалось
в лог (`replica_guard.check_single_replica`), которого никто не читает.

Хуже лимитов — сторожа. `_reset_stale_running` на старте и `_watchdog_stale`
каждую минуту возвращают в очередь всё, что висит в 'running'. Второй процесс
считал бы ЖИВЫЕ операции первого жертвами падения: тот пошёл бы выполнять их
второй раз (реальные действия — приглашения, посты, сообщения людям), а бюджет
живучести списался бы впустую, и здоровая многочасовая операция объявлялась бы
ядовитой.

Теперь это держится арендой: строка `executor_lease` (id=1) со сроком. Кто её
держит — разбирает очередь; остальные ждут. Срок истёк (процесс умер) или аренду
отпустили при плановой остановке — берёт следующий.

Запросы проверяются на НАСТОЯЩЕМ Postgres: заглушка пула не проверяет ни типы
параметров, ни семантику `ON CONFLICT ... WHERE`, то есть главное свойство
аренды — «второй не получает строку» — на юнит-заглушке не проверяется в
принципе. Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
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


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM executor_lease"))
    _run(pool.execute("DELETE FROM process_heartbeats"))


def test_second_executor_does_not_get_the_lease(pool):
    """Главное свойство: вторым процесс очередь не получает."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    assert _run(rg.acquire_executor_lease(pool, "host:2:bbb", "worker")) is False
    assert _run(rg.executor_lease_holder(pool)) == "host:1:aaa"


def test_holder_renews_its_own_lease(pool):
    """Повторный вызов держателя — продление, а не отказ себе же."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    row = _run(pool.fetchrow("SELECT acquired_at, renewed_at FROM executor_lease WHERE id=1"))
    assert row["renewed_at"] >= row["acquired_at"], (
        "продление обязано двигать renewed_at, а не acquired_at: иначе по строке "
        "не видно, как долго процесс держит очередь")


def test_lease_frees_itself_when_the_holder_dies(pool):
    """Умерший держатель не запирает очередь навсегда — только до истечения срока."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    # держатель «умер»: срок истёк
    _run(pool.execute(
        "UPDATE executor_lease SET expires_at = now() - make_interval(secs => 1) WHERE id=1"))
    assert _run(rg.executor_lease_holder(pool)) is None
    assert _run(rg.acquire_executor_lease(pool, "host:2:bbb", "worker")) is True
    assert _run(rg.executor_lease_holder(pool)) == "host:2:bbb"


def test_planned_stop_hands_the_queue_over_at_once(pool):
    """Деплой не должен останавливать очередь на срок аренды."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    _run(rg.release_executor_lease(pool, "host:1:aaa"))
    assert _run(rg.acquire_executor_lease(pool, "host:2:bbb", "worker")) is True


def test_nobody_releases_a_lease_that_is_not_his(pool):
    """Чужую аренду отпустить нельзя — иначе любой процесс отнимает очередь."""
    from services import replica_guard as rg

    assert _run(rg.acquire_executor_lease(pool, "host:1:aaa", "worker")) is True
    _run(rg.release_executor_lease(pool, "host:2:bbb"))
    assert _run(rg.executor_lease_holder(pool)) == "host:1:aaa"
    assert _run(rg.acquire_executor_lease(pool, "host:2:bbb", "worker")) is False


# ── Счёт реплик: предупреждение о риске бана относится к ИСПОЛНИТЕЛЯМ ────────

def test_web_plus_worker_is_not_a_double_executor(pool):
    """Штатная раскладка web+worker — две реплики и ОДИН исполнитель.

    Ровно на этой раскладке страж кричал «ЗАПУЩЕНО 2 РЕПЛИК … прямой риск бана»
    каждые тридцать секунд. Ложная тревога дороже молчания: на неё перестают
    смотреть, и настоящий случай двух worker-ов проходит незамеченным.
    """
    from services import replica_guard as rg

    _run(rg.beat(pool, "host:1:web", "web"))
    _run(rg.beat(pool, "host:2:wrk", "worker"))
    assert _run(rg.active_process_count(pool)) == 2
    assert _run(rg.active_replica_count(pool)) == 1
    assert _run(rg.check_single_replica(pool)) == 1


def test_two_workers_are_counted(pool):
    """А вот это — настоящий случай, и он обязан считаться."""
    from services import replica_guard as rg

    _run(rg.beat(pool, "host:1:wrk", "worker"))
    _run(rg.beat(pool, "host:2:wrk", "all"))
    assert _run(rg.active_replica_count(pool)) == 2
    assert _run(rg.check_single_replica(pool)) == 2
