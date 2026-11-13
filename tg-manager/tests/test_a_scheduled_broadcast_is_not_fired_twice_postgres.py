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


@pytest.mark.parametrize("change,expected", [
    ("pause", "pending"), ("archive", "cancelled"), ("rotate", "pending"),
    ("revoke_share", "cancelled"), ("none", "done"),
])
def test_scheduler_rechecks_admission_after_telegram_preflight(pool, monkeypatch, change, expected):
    """Даже успешный getMe не разрешает отправку после смены допуска."""
    from unittest.mock import Mock

    from services import scheduler

    class StopScheduler(Exception):
        pass

    async def stop_after_tick(seconds):
        assert seconds == 60
        raise StopScheduler

    async def scenario():
        schedule_owner = OWNER
        workspace = None
        if change == "revoke_share":
            await pool.execute(
                "INSERT INTO platform_users(user_id) VALUES($1),($2) ON CONFLICT DO NOTHING",
                OWNER, OWNER + 1)
            workspace = await pool.fetchval(
                "INSERT INTO workspaces(owner_id, name) VALUES($1,'Тестовый доступ') RETURNING id", OWNER)
            schedule_owner = OWNER + 1
            await pool.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role) VALUES($1,$2,'editor')",
                workspace, schedule_owner)
        schedule = await pool.fetchval(
            """INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, repeat_interval_min)
               VALUES($1,'Проверка',now()-interval '1 minute',$2,60) RETURNING id""", BOT, schedule_owner)
        token = f"test-token-{BOT}"
        start = Mock()

        async def preflight(http, actual_token):
            assert actual_token == token
            if change == "pause":
                await pool.execute("UPDATE managed_bots SET is_active=FALSE WHERE bot_id=$1", BOT)
            elif change == "archive":
                await pool.execute(
                    "UPDATE managed_bots SET is_active=FALSE, deleted_at=now() WHERE bot_id=$1", BOT)
            elif change == "rotate":
                await pool.execute("UPDATE managed_bots SET token='rotated' WHERE bot_id=$1", BOT)
            elif change == "revoke_share":
                await pool.execute("DELETE FROM workspace_members WHERE workspace_id=$1", workspace)
            return "ok"

        try:
            monkeypatch.setattr(scheduler, "_token_state", preflight)
            monkeypatch.setattr(scheduler.broadcaster, "start", start)
            monkeypatch.setattr(scheduler.asyncio, "sleep", stop_after_tick)
            with pytest.raises(StopScheduler):
                await scheduler.run(pool, None)
            row = await pool.fetchrow(
                "SELECT status, broadcast_id FROM scheduled_broadcasts WHERE id=$1", schedule)
            assert row["status"] == expected
            if change == "none":
                start.assert_called_once()
                assert start.call_args.kwargs["schedule_creator_id"] == schedule_owner
                assert row["broadcast_id"] is not None
                assert await pool.fetchval(
                    "SELECT scheduled_id FROM broadcasts WHERE id=$1", row["broadcast_id"]) == schedule
                assert await pool.fetchval(
                    "SELECT count(*) FROM scheduled_broadcasts WHERE bot_id=$1 AND status='pending'",
                    BOT) == 1
            else:
                start.assert_not_called()
                assert row["broadcast_id"] is None
                assert await pool.fetchval("SELECT count(*) FROM broadcasts WHERE scheduled_id=$1", schedule) == 0
        finally:
            await pool.execute(
                "UPDATE managed_bots SET is_active=TRUE, deleted_at=NULL, token=$2 WHERE bot_id=$1",
                BOT, token,
            )
            if workspace is not None:
                await pool.execute("DELETE FROM workspaces WHERE id=$1", workspace)
            await pool.execute("DELETE FROM scheduled_broadcasts WHERE id=$1", schedule)

    _run(scenario())


def test_archive_during_broadcast_preflight_cannot_revive_cancellation(pool, monkeypatch):
    from unittest.mock import AsyncMock

    from database import db
    from services import broadcaster

    async def scenario():
        token = "778900:synthetic_token_for_postgres_tests_0123456789"
        old_token = f"test-token-{BOT}"
        await pool.execute("UPDATE managed_bots SET token=$2 WHERE bot_id=$1", BOT, token)
        schedule = await _schedule(pool)
        broadcast = await db.create_broadcast(pool, BOT, "Привет", 1, OWNER, scheduled_id=schedule)

        async def get_me(*args):
            await pool.execute(
                "UPDATE broadcasts SET status='cancelled', status_note='Архивирование' WHERE id=$1",
                broadcast)
            return {"id": BOT}

        send = AsyncMock()
        monkeypatch.setattr(broadcaster.bot_api, "get_me", get_me)
        monkeypatch.setattr(broadcaster.bot_api, "send_message_classified", send)
        try:
            await broadcaster.run(
                pool, object(), broadcast, token, BOT, "Привет", user_ids=[100],
                schedule_creator_id=OWNER,
            )
            send.assert_not_awaited()
            row = await pool.fetchrow("SELECT status, status_note FROM broadcasts WHERE id=$1", broadcast)
            assert tuple(row) == ("cancelled", "Архивирование")
        finally:
            await pool.execute("UPDATE managed_bots SET token=$2 WHERE bot_id=$1", BOT, old_token)

    _run(scenario())
