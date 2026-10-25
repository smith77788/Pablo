"""Два параллельных захода не отправляют шаг воронки дважды.

Отметка «шаг отправлен» (`last_sent_step`) ставится условным UPDATE'ом ДО
отправки — именно чтобы рестарт между отправкой и продвижением не повторил
сообщение живому человеку. На заглушках этого не докажешь: вся суть в том,
что условие и запись происходят в ОДНОМ запросе, то есть атомарно для базы.
Поэтому проверка идёт в живой Postgres и запускает два захода по одной строке
одновременно: отправка обязана случиться ровно один раз.

Почему это не теория: процессов, которые могут одновременно оказаться на
одной строке, два (web и воркер), а рабочая ветка уезжает в прод на каждом
пуше, так что перезапуск посреди круга — норма, а не редкость.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993437
BOT_ID = 880099773
USER_ID = 5150

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _cleanup(p):
    _run(p.execute(
        "DELETE FROM auto_funnel_runs WHERE funnel_id IN "
        "(SELECT id FROM auto_funnels WHERE owner_id=$1)", OWNER))
    _run(p.execute("DELETE FROM auto_funnels WHERE owner_id=$1", OWNER))


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=2, max_size=6)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _cleanup(p)
    yield p
    _cleanup(p)
    _run(p.close())


@pytest.fixture
def funnel(pool):
    _cleanup(pool)
    fid = _run(pool.fetchval(
        "INSERT INTO auto_funnels(owner_id, name, bot_id) VALUES($1,$2,$3) "
        "RETURNING id", OWNER, "тест гонки", BOT_ID))
    _run(pool.execute(
        "INSERT INTO auto_funnel_steps(funnel_id, step_num, delay_hours, message_text) "
        "VALUES($1,1,0,$2),($1,2,24,$3)", fid, "первый шаг", "второй шаг"))
    rid = _run(pool.fetchval(
        "INSERT INTO auto_funnel_runs(funnel_id, bot_id, user_id, next_step_num, "
        "next_send_at) VALUES($1,$2,$3,1,NOW()) RETURNING id",
        fid, BOT_ID, USER_ID))
    return fid, rid


@pytest.fixture
def sends(monkeypatch):
    from services import bot_api
    from database import db

    seen: list[int] = []

    async def _send(session, token, chat_id, text, **kw):
        # Пауза внутри отправки растягивает окно между отметкой и записью
        # продвижения — ровно там, где раньше умирал процесс.
        await asyncio.sleep(0.05)
        seen.append(int(chat_id))
        return True, 0, "ok"

    async def _bot_row(pool, query, *a):
        return {"token": "123456789:AAFqwertyuiopasdfghjklzxcvbnm123456A"}

    async def _send_plain(session, token, chat_id, text, **kw):
        ok, _retry, _cat = await _send(session, token, chat_id, text, **kw)
        return ok

    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    # Старый путь звал `send_message`: подменяем и его, иначе доказательство
    # «без фикса шаг уходит дважды» краснело бы по другой причине.
    monkeypatch.setattr(bot_api, "send_message", _send_plain)
    monkeypatch.setattr(db, "fetchrow_bot", _bot_row, raising=False)
    return seen


def test_only_one_of_two_parallel_passes_sends_the_step(pool, funnel, sends):
    from services import auto_funnel

    fid, rid = funnel
    row = {"id": rid, "funnel_id": fid, "bot_id": BOT_ID, "user_id": USER_ID,
           "next_step_num": 1}

    async def _both():
        await asyncio.gather(
            auto_funnel._process_run(pool, object(), dict(row)),
            auto_funnel._process_run(pool, object(), dict(row)),
        )

    _run(_both())

    assert sends == [USER_ID], (
        f"человек получил шаг {len(sends)} раз(а) вместо одного")
    left = _run(pool.fetchrow(
        "SELECT next_step_num, last_sent_step FROM auto_funnel_runs WHERE id=$1",
        rid))
    assert left["last_sent_step"] == 1
    assert left["next_step_num"] == 2, "воронка не продвинулась к следующему шагу"


def test_a_second_pass_after_a_restart_skips_the_sent_step(pool, funnel, sends):
    """Так выглядит рестарт: отметка в базе есть, продвижения ещё нет."""
    from services import auto_funnel

    fid, rid = funnel
    _run(pool.execute(
        "UPDATE auto_funnel_runs SET last_sent_step=1 WHERE id=$1", rid))

    _run(auto_funnel._process_run(pool, object(), {
        "id": rid, "funnel_id": fid, "bot_id": BOT_ID, "user_id": USER_ID,
        "next_step_num": 1}))

    assert sends == [], "после рестарта шаг ушёл человеку второй раз"
    nxt = _run(pool.fetchval(
        "SELECT next_step_num FROM auto_funnel_runs WHERE id=$1", rid))
    assert nxt == 2, "воронка застряла: шаг пропущен, но продвижения нет"
