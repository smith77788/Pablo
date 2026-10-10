"""Предохранитель операций — ОДИН на весь продукт, а не по одному на процесс.

ЗАЧЕМ ОТДЕЛЬНО (находка аудита №3). Счётчик сбоев жил в словаре уровня модуля.
Пока процесс был один, это работало; после разделения ролей и с несколькими
воркерами предохранитель ломается ровно там, где он нужнее всего:
  • каждая реплика считает сбои отдельно — порог «3 подряд» становится 3×N,
    и флот жжёт аккаунты втрое дольше, прежде чем встать на паузу;
  • сработавшая на реплике A пауза не останавливает реплику B;
  • роль web читает СВОЮ пустую память и показывает «всё в порядке», пока
    воркер стоит на паузе.

Заглушкой пула это невоспроизводимо: нужна настоящая БД, где две реплики
конкурируют за одну строку. Две реплики моделируются честно — два независимых
экземпляра модуля op_worker, у каждого своя память, база одна.

Запуск: INFRAGRAM_TEST_DSN=... pytest tests/test_circuit_breaker_shared_postgres.py
Без переменной файл пропускается.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 993311

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _fresh_op_worker(tag: str):
    """Независимый экземпляр предохранителя — своя память, как отдельный процесс.

    Предохранитель вынесен в services/op_circuit_breaker.py; грузим ИМЕННО его
    (а не op_worker) — так у каждой «реплики» свой _circuit_breaker_state и _db_pool,
    а база (op_circuit_breaker) остаётся общей.
    """
    path = os.path.join(ROOT, "services", "op_circuit_breaker.py")
    spec = importlib.util.spec_from_file_location(f"_cbreplica_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def stand():
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS cbtest")
        finally:
            await setup.close()
        pool = await asyncpg.create_pool(
            DSN, min_size=2, max_size=8,
            server_settings={"search_path": "cbtest"})
        # Ровно та миграция, что уезжает в прод. Комментарии идут ПЕРЕД первым
        # ';', поэтому чанк целиком отбрасывать нельзя — чистим построчно.
        with open(os.path.join(ROOT, "schema_v182.sql"), encoding="utf-8") as f:
            for chunk in f.read().split(";"):
                body = "\n".join(ln for ln in chunk.split("\n")
                                 if not ln.strip().startswith("--")).strip()
                if body:
                    await pool.execute(body)
        return pool

    pool = _run(_mk())
    a, b = _fresh_op_worker("a"), _fresh_op_worker("b")
    a.set_pool(pool)
    b.set_pool(pool)
    yield pool, a, b
    _run(pool.execute("DROP TABLE IF EXISTS op_circuit_breaker"))
    _run(pool.close())


@pytest.fixture(autouse=True)
def _clean(stand):
    pool, a, b = stand
    _run(pool.execute("DELETE FROM op_circuit_breaker WHERE owner_id=$1", OWNER))
    a._circuit_breaker_state.clear()
    b._circuit_breaker_state.clear()
    yield


def _db_failures(pool) -> int:
    row = _run(pool.fetchrow(
        "SELECT failures FROM op_circuit_breaker WHERE owner_id=$1", OWNER))
    return int(row["failures"]) if row else 0


# ── ядро ─────────────────────────────────────────────────────────────────────

def test_failures_from_two_replicas_add_up_to_one_threshold(stand):
    """Порог общий: 2 сбоя у A + 1 у B обязаны сложиться в 3 и открыть цепь.

    Раньше каждая реплика считала своё — до паузы нужно было 3 сбоя НА КАЖДОЙ,
    то есть 3×N операций сгорало впустую.
    """
    pool, a, b = stand
    assert a._CIRCUIT_BREAKER_THRESHOLD == 3, "тест написан под порог 3"

    _run(a._circuit_breaker_record(OWNER, False))
    _run(a._circuit_breaker_record(OWNER, False))
    assert _db_failures(pool) == 2, "сбои реплики A не попали в общее состояние"

    tripped = _run(b._circuit_breaker_record(OWNER, False))   # третий — у СОСЕДА
    assert _db_failures(pool) == 3, (
        f"счётчик не сложился между репликами: в БД {_db_failures(pool)} вместо 3")
    assert tripped, "порог достигнут суммарно, но цепь не открылась"


def test_trip_on_one_replica_stops_the_other(stand):
    """Пауза, поставленная A, обязана остановить B — иначе она бесполезна."""
    pool, a, b = stand
    for _ in range(3):
        _run(a._circuit_breaker_record(OWNER, False))
    assert _run(a.circuit_breaker_is_open(OWNER)), "цепь не открылась у самой A"

    # У B в памяти пусто — как у только что стартовавшей реплики.
    assert not b._circuit_breaker_state, "предусловие: память соседа пуста"
    assert _run(b.circuit_breaker_is_open(OWNER)), (
        "сосед не видит паузу и продолжит запускать операции в тот же шторм")
    st = _run(b.circuit_breaker_status(OWNER))
    assert st["status"] == "open" and st["cooldown_remaining_s"] > 0, st


def test_web_role_sees_the_pause_worker_set(stand):
    """Роль web операций не запускает — её память пуста ВСЕГДА.

    Именно поэтому мини-апп показывал «всё в порядке», пока воркер стоял на
    паузе: он читал кэш процесса. Общий читатель обязан отдать правду.
    """
    _pool, worker, web = stand
    for _ in range(3):
        _run(worker._circuit_breaker_record(OWNER, False))

    assert web._circuit_breaker_status(OWNER)["status"] == "closed", (
        "предусловие: синхронный кэш web-процесса пуст")
    assert _run(web.circuit_breaker_status(OWNER))["status"] == "open", (
        "мини-апп не покажет паузу — снаружи это «операции просто не идут»")


def test_success_on_any_replica_closes_the_circuit(stand):
    """Восстановление тоже общее: успех у B снимает паузу, поставленную A."""
    _pool, a, b = stand
    for _ in range(3):
        _run(a._circuit_breaker_record(OWNER, False))
    assert _run(a.circuit_breaker_is_open(OWNER))

    _run(b._circuit_breaker_record(OWNER, True))    # успех у соседа
    assert not _run(a.circuit_breaker_is_open(OWNER)), (
        "аккаунты снова работают, но пауза держится до конца cooldown")


def test_concurrent_records_do_not_lose_increments(stand):
    """Одновременные записи не теряют инкременты.

    Без блокировки строки «прочитал-посчитал-записал» двух реплик даёт +1
    вместо +2 — порог размывается ровно так же, как при памяти процесса.
    """
    pool, a, b = stand

    async def go():
        await asyncio.gather(*[
            m._circuit_breaker_record(OWNER, False)
            for m in (a, b) for _ in range(5)
        ])

    _run(go())
    assert _db_failures(pool) == 10, (
        f"потеряны инкременты: в БД {_db_failures(pool)} вместо 10")


def test_expired_cooldown_resets_shared_state(stand):
    """Истёкший cooldown закрывает цепь и обнуляет счётчик — в общем состоянии."""
    pool, a, b = stand
    for _ in range(3):
        _run(a._circuit_breaker_record(OWNER, False))
    _run(pool.execute(
        "UPDATE op_circuit_breaker SET cooldown_until = now() - interval '1 second' "
        "WHERE owner_id=$1", OWNER))

    assert not _run(b.circuit_breaker_is_open(OWNER)), "пауза не кончилась по времени"
    assert _run(b.circuit_breaker_status(OWNER)) == {"status": "closed", "failures": 0}
    _run(b._circuit_breaker_record(OWNER, False))
    assert _db_failures(pool) == 1, (
        "после истёкшего cooldown счёт обязан начаться заново, а не продолжиться")


def test_db_outage_falls_back_to_local_counter(stand):
    """БД недоступна — считаем в памяти, а не блокируем работу владельца.

    Предохранитель, в отличие от аренды аккаунтов, fail-OPEN: ошибочная пауза
    остановила бы всю работу, а цена пропущенной паузы — лишние сбои.
    """
    _pool, a, _b = stand
    saved = a._db_pool
    a._db_pool = None            # как будто БД отвалилась
    try:
        for _ in range(3):
            tripped = _run(a._circuit_breaker_record(OWNER, False))
        assert tripped, "без БД предохранитель перестал работать вовсе"
        assert a._circuit_breaker_is_open(OWNER)
    finally:
        a._db_pool = saved
