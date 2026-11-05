"""Отложенная рассылка не уходит второй раз — проверка на живом Postgres.

САМЫЙ ОПАСНЫЙ ЗАЗОР ПЛАНИРОВЩИКА. Он занимает строку расписания
(`status='processing'`), создаёт рассылку, ОТДЕЛЬНЫМ запросом пишет её id в
`scheduled_broadcasts.broadcast_id` и запускает залп. Процесс умирает между
созданием и записью id — на деплое это штатное событие — и дальше:

  * рассылка уже стоит в 'pending', а такие на следующем старте процесса
    поднимает `broadcaster.resume_interrupted`: сообщение УЙДЁТ;
  * расписание осталось в 'processing' с пустым `broadcast_id`, и
    восстановление, поверив этой пустоте, вернуло бы его в очередь — завелась
    бы ВТОРАЯ рассылка на ту же аудиторию, и все получили бы сообщение дважды.

Лечение — обратная ссылка `broadcasts.scheduled_id` (schema_v298), которая
пишется ТЕМ ЖЕ INSERT, что и сама рассылка, поэтому своего зазора не имеет.
Здесь это проверяется на настоящей базе: заглушка «вернула бы 999» на любой
вопрос и не доказала бы ни колонку, ни запрос.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889934
BOT = 778900

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

    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass          # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def clean(pool):
    """Своё убираем ДО и ПОСЛЕ: чужая строка с тем же расписанием солгала бы.

    Бот нужен настоящий: у `scheduled_broadcasts.bot_id` есть внешний ключ на
    `managed_bots`, и на заглушке этого видно не было.
    """
    async def _wipe():
        await pool.execute("DELETE FROM broadcasts WHERE created_by = $1", OWNER)
        await pool.execute(
            "DELETE FROM scheduled_broadcasts WHERE created_by = $1", OWNER)

    async def _prepare():
        await _wipe()
        await pool.execute(
            "INSERT INTO managed_bots (token, bot_id, added_by) VALUES ($1,$2,$3) "
            "ON CONFLICT DO NOTHING",
            f"test-token-{BOT}", BOT, OWNER)

    _run(_prepare())
    yield
    _run(_wipe())


async def _schedule(pool, *, broadcast_id=None) -> int:
    """Занятая строка расписания, брошенная «умершим процессом»."""
    return await pool.fetchval(
        """INSERT INTO scheduled_broadcasts
               (bot_id, message_text, execute_at, created_by, status,
                claimed_at, broadcast_id)
           VALUES ($1, 'привет', now() - interval '1 hour', $2, 'processing',
                   now() - interval '1 hour', $3)
        RETURNING id""",
        BOT, OWNER, broadcast_id)


def test_the_link_survives_in_the_same_insert(pool):
    """Ссылка на расписание ложится вместе с рассылкой и читается обратно."""
    from database import db

    async def _go():
        sched_id = await _schedule(pool)
        bc_id = await db.create_broadcast(
            pool, BOT, "привет", 10, OWNER, scheduled_id=sched_id)
        found = await pool.fetchval(
            "SELECT id FROM broadcasts WHERE scheduled_id = $1 ORDER BY id LIMIT 1",
            sched_id)
        return bc_id, found

    bc_id, found = _run(_go())
    assert found == bc_id, (
        "рассылка не находится по расписанию — восстановлению снова нечего "
        "спросить, и второй залп на всю аудиторию возможен")


def test_a_broadcast_without_its_link_closes_the_schedule(pool):
    """Рассылка есть, `broadcast_id` пуст — расписание закрывается, а не повторяется."""
    from services import scheduler

    async def _go():
        sched_id = await _schedule(pool)          # broadcast_id = NULL
        bc_id = await pool.fetchval(
            """INSERT INTO broadcasts
                   (bot_id, message_text, total_users, status, created_by, scheduled_id)
               VALUES ($1, 'привет', 10, 'pending', $2, $3) RETURNING id""",
            BOT, OWNER, sched_id)
        stats = await scheduler.recover_stuck_claims(pool)
        row = await pool.fetchrow(
            "SELECT status, broadcast_id FROM scheduled_broadcasts WHERE id = $1",
            sched_id)
        return stats, row, bc_id

    stats, row, bc_id = _run(_go())
    assert stats["closed"] >= 1 and stats["requeued"] == 0, (
        "расписание с уже созданной рассылкой вернулось в очередь — "
        f"это сообщение дважды всей аудитории ({stats})")
    assert row["status"] == "done"
    assert row["broadcast_id"] == bc_id, (
        "связь не дописана: следующий сторож спросит то же самое снова")


def test_a_schedule_with_no_broadcast_still_returns_to_the_queue(pool):
    """Обратная сторона: работы правда не было — расписание возвращается в очередь.

    Без этой проверки «закрывать всегда» прошло бы предыдущий тест и тихо
    съедало бы отложенные рассылки, которые ещё не уходили.
    """
    from services import scheduler

    async def _go():
        sched_id = await _schedule(pool)          # ни рассылки, ни ссылки
        stats = await scheduler.recover_stuck_claims(pool)
        row = await pool.fetchrow(
            "SELECT status FROM scheduled_broadcasts WHERE id = $1", sched_id)
        return stats, row

    stats, row = _run(_go())
    assert stats["requeued"] >= 1, f"брошенное расписание не вернулось в очередь ({stats})"
    assert row["status"] == "pending"
