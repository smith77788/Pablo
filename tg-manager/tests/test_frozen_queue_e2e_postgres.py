"""Детектор «очередь не разбирается» — на НАСТОЯЩЕМ Postgres.

ЗАЧЕМ ОТДЕЛЬНО ОТ ЮНИТ-ТЕСТОВ. Заглушка пула принимает любые параметры, поэтому
ошибки СВЯЗЫВАНИЯ (строка вместо timestamptz, float вместо interval) на
юнит-тестах невидимы в принципе — так пережил релиз сломанный
`operation_bus.submit(scheduled_for=…)`, убивавший все отложенные операции.

Цена такой ошибки именно здесь особенно высока: `queue_frozen` намеренно
fail-open (любой сбой запроса → None, «всё в порядке»). Сломанный запрос не упал
бы и не залогировался бы громко — детектор просто молчал бы всегда, то есть
остановка продукта снова стала бы невидимой, причём с зелёными тестами.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py (тот же стенд,
та же переменная INFRAGRAM_TEST_DSN). Без переменной файл пропускается.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990201
OWNER2 = 990202

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
    # Убрать за собой и на выходе: иначе ждущие строки этого файла остаются в
    # базе и ломают следующий файл, который спросит очередь целиком.
    _run(_clean(p))
    _run(p.close())


async def _clean(p):
    from services import operation_bus as obus

    await p.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, OWNER2])
    await p.execute("DELETE FROM platform_settings WHERE key=$1", obus.POLLER_MARK_KEY)


async def _assert_quiet(p, why: str = "") -> None:
    """Детектор обязан молчать — а если не молчит, сказать ПОЧЕМУ.

    `queue_frozen` спрашивает очередь ЦЕЛИКОМ, без скоупа по владельцу: «вся
    работа встала» — вопрос системный, и это правильно. Зато любая строка,
    оставленная чужим тестом, роняет здесь «пустая очередь → None», и выглядит
    это как сломанный детектор; разбор уходит в сторону. Чужого владельца
    чистить нельзя — порядок тестов не наш, — поэтому называем виновного в
    тексте провала. Так нашёлся тест запуска кампании: он убирал кампании, а
    пять 'pending' оставлял в очереди навсегда.
    """
    from services import operation_bus as obus

    verdict = await obus.queue_frozen(p)
    if verdict is None:
        return
    foreign = await p.fetch(
        "SELECT owner_id, count(*) AS n FROM operation_queue "
        "WHERE owner_id <> ALL($1::bigint[]) GROUP BY 1 ORDER BY 2 DESC",
        [OWNER, OWNER2])
    note = ""
    if foreign:
        note = (" В очереди лежат ЧУЖИЕ строки (владелец → сколько): "
                + ", ".join(f"{r['owner_id']} → {r['n']}" for r in foreign)
                + " — похоже, чей-то тест не убрал за собой operation_queue, "
                  "а детектор смотрит очередь целиком.")
    raise AssertionError(
        (why or "детектор обязан молчать") + f", а он вернул {verdict}." + note)


async def _enqueue(p, owner: int, *, age_min: int = 0, scheduled_in_min=None,
                   approval: bool = False, status: str = "pending") -> int:
    """Строка очереди с заданным возрастом — так, как её видит детектор."""
    sched = (f"now() + make_interval(mins => {int(scheduled_in_min)})"
             if scheduled_in_min is not None else "NULL")
    return await p.fetchval(
        f"""INSERT INTO operation_queue
                (owner_id, op_type, status, params, total_items, label,
                 created_at, scheduled_for, requires_approval)
            VALUES ($1, 'contacts_sync', $2, $3::jsonb, 1, 'e2e',
                    now() - make_interval(mins => $4), {sched}, $5)
            RETURNING id""",
        owner, status, json.dumps({}), int(age_min), approval)


def test_the_query_runs_at_all(pool):
    """Первое и главное: запрос выполним на живом драйвере."""
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        # Пустая очередь — верный ответ None, но получен он ДОЛЖЕН быть от
        # Postgres, а не от except-ветки fail-open.
        await _assert_quiet(pool)

        await _enqueue(pool, OWNER, age_min=30)
        verdict = await obus.queue_frozen(pool)
        assert verdict is not None, (
            "работа ждёт полчаса, отметки поллера нет — детектор обязан "
            "сработать; молчание здесь значит, что запрос упал и был съеден "
            "fail-open"
        )
        assert verdict["owners"] == {OWNER: 1}
        assert verdict["silence_s"] is None
        assert verdict["oldest_min"] == 30

    _run(_t())


def test_a_real_mark_silences_the_detector(pool):
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        await _enqueue(pool, OWNER, age_min=30)
        await obus.mark_poller_alive(pool, "host:1:abc")

        await _assert_quiet(
            pool, "поллер отметился только что, а детектор всё равно объявил остановку")
        # Та же отметка, сдвинутая в прошлое часами БД.
        await pool.execute(
            "UPDATE platform_settings SET updated_at = now() - interval '20 minutes' "
            "WHERE key=$1", obus.POLLER_MARK_KEY)
        verdict = await obus.queue_frozen(pool)
        assert verdict is not None
        assert 1100 < verdict["silence_s"] < 1300, verdict["silence_s"]

    _run(_t())


def test_the_mark_is_an_upsert_not_a_second_row(pool):
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        for i in range(3):
            await obus.mark_poller_alive(pool, f"host:{i}:x")
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM platform_settings WHERE key=$1", obus.POLLER_MARK_KEY)
        assert n == 1, "отметка плодит строки — вторая запись упала бы на PRIMARY KEY"
        val = await pool.fetchval(
            "SELECT value FROM platform_settings WHERE key=$1", obus.POLLER_MARK_KEY)
        assert val == "host:2:x", "отметка не обновляется — возраст будет врать"

    _run(_t())


def test_legitimate_waiting_is_not_a_stall(pool):
    """Отложенная операция и операция на подтверждении остановкой не считаются."""
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        # Создана час назад, но срок ещё не наступил: флуд-пауза, «продолжим
        # завтра», повтор после cooldown — всё это законное ожидание.
        await _enqueue(pool, OWNER, age_min=60, scheduled_in_min=120)
        await _assert_quiet(pool)

        await _clean(pool)
        await _enqueue(pool, OWNER, age_min=60, approval=True)
        await _assert_quiet(
            pool, "операция ждёт подтверждения владельца, а детектор счёл это аварией")

        await _clean(pool)
        await _enqueue(pool, OWNER, age_min=60, status="paused")
        await _assert_quiet(
            pool, "приостановленная владельцем очередь — не остановка исполнителя")

    _run(_t())


def test_a_deferred_operation_counts_once_its_time_has_come(pool):
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        # Срок наступил 40 минут назад — теперь это реально ждущая работа, и
        # возраст ожидания считается от срока, а не от создания.
        await _enqueue(pool, OWNER, age_min=600, scheduled_in_min=-40)
        verdict = await obus.queue_frozen(pool)
        assert verdict is not None
        assert verdict["oldest_min"] == 40, (
            f"возраст ожидания считается не от наступившего срока: {verdict}"
        )

    _run(_t())


def test_every_affected_owner_is_named(pool):
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        await _enqueue(pool, OWNER, age_min=20)
        await _enqueue(pool, OWNER, age_min=25)
        await _enqueue(pool, OWNER2, age_min=10)
        # Свежая работа под порогом в счёт не идёт.
        await _enqueue(pool, OWNER2, age_min=1)

        verdict = await obus.queue_frozen(pool)
        assert verdict["owners"] == {OWNER: 2, OWNER2: 1}, verdict
        assert verdict["pending"] == 3

    _run(_t())


def test_the_threshold_is_the_one_from_the_module(pool):
    from services import operation_bus as obus

    async def _t():
        await _clean(pool)
        # Ровно под порогом молчания — ещё не авария.
        await _enqueue(pool, OWNER, age_min=max(1, obus.POLLER_SILENCE_S // 60 - 2))
        await _assert_quiet(pool)
        await _enqueue(pool, OWNER, age_min=obus.POLLER_SILENCE_S // 60 + 2)
        assert await obus.queue_frozen(pool) is not None

    _run(_t())
