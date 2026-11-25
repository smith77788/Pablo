"""A/B-рассылка существует — и теперь у неё есть дверь. Живая база.

РАЗРЫВ. `broadcaster.ab_test_broadcast` написан, ставит по операции на
вариант, делит аудиторию и даже умеет бросать недоставленный вариант
(`abandon_unstarted`). Ручка `/api/miniapp/broadcast/ab_test` зарегистрирована.
А экрана нет НИ ОДНОГО: ни одна строка `mini_app/` этот адрес не звала —
то есть продукт умел сравнивать два текста рассылки, и владелец об этом
узнать не мог. Это ровно тот класс, которым семь готовых разделов уже были
недоступны (каталог «Все функции»).

Второй дефект того же места: отказы движка владелец читал по-английски
(«Bot not found», «No active subscribers») — они доезжают до экрана как есть,
а владелец английский не читает.

Третий: A/B звал `db.get_audience_user_ids` (ВСЕХ активных) и не знал ни про
сегмент, ни про кнопки, ни про тихую отправку, ни про отсрочку. Экран рассылки
всё это обещает, поэтому «Спящим, через 3 часа» ушло бы всем и сразу. Условие
сегмента жило внутри исполнителя — теперь это одна дверь
(`broadcaster.broadcast_audience_ids`), которую читают оба пути.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995931
BOT = 880099931


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    # Квота бесплатного тарифа живёт в отдельной таблице и НЕ исчезает вместе
    # с операциями: без этой уборки второй прогон файла падает отказом
    # «бесплатные операции закончились», а причина выглядит как дефект кода.
    await pool.execute(
        "DELETE FROM free_op_usage WHERE owner_id = ANY($1::bigint[])", [OWNER, OWNER + 1])
    await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT)
    await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
    await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)


async def _seed(pool, sleepers: int = 4, fresh: int = 6):
    await _clean(pool)
    await pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,$3,TRUE,$4)", f"1234:ab-test-{BOT}", BOT, "ab_test_bot", OWNER)
    for i in range(fresh):
        await pool.execute(
            "INSERT INTO bot_users(bot_id, user_id, is_active, is_blocked, suspect, last_seen) "
            "VALUES($1,$2,TRUE,FALSE,FALSE, now())", BOT, 700000 + i)
    for i in range(sleepers):
        await pool.execute(
            "INSERT INTO bot_users(bot_id, user_id, is_active, is_blocked, suspect, last_seen) "
            "VALUES($1,$2,TRUE,FALSE,FALSE, now() - interval '40 days')", BOT, 800000 + i)


def _ab(**kw):
    import asyncpg

    from services.broadcaster import ab_test_broadcast

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool)
            res = await ab_test_broadcast(
                pool, OWNER, BOT,
                [{"text": "Вариант А", "weight": 1}, {"text": "Вариант Б", "weight": 1}],
                **kw)
            ops = await pool.fetch(
                "SELECT params, total_items, scheduled_for FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='run_broadcast' ORDER BY id", OWNER)
            return res, [dict(o) for o in ops]
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


def _ids(ops) -> list[int]:
    import json
    out = []
    for o in ops:
        p = o["params"]
        p = json.loads(p) if isinstance(p, str) else p
        out += [int(x) for x in (p.get("user_ids") or [])]
    return out


def test_the_split_covers_everyone_exactly_once():
    res, ops = _ab()
    assert res["ok"], res
    assert len(ops) == 2, "на каждый вариант должна стоять своя рассылка"
    ids = _ids(ops)
    assert len(ids) == 10, ids
    assert len(set(ids)) == 10, "человек попал в два варианта сразу — тест сравнивал бы себя"
    assert res["total_users"] == 10


def test_the_segment_is_respected_and_not_quietly_widened():
    """«Спящим» обязано означать спящих: иначе экран обещает одно, уходит другое."""
    res, ops = _ab(segment="inactive_14d")
    assert res["ok"], res
    ids = _ids(ops)
    assert len(ids) == 4, f"в сегменте четверо спящих, а выбрано {len(ids)}"
    assert all(i >= 800000 for i in ids), "в A/B попали свежие подписчики мимо сегмента"


def test_buttons_silence_and_delay_reach_the_executor():
    import json
    res, ops = _ab(buttons=[{"text": "Купить", "url": "https://example.com"}],
                   silent=True, schedule_minutes=180)
    assert res["ok"], res
    for o in ops:
        p = o["params"]
        p = json.loads(p) if isinstance(p, str) else p
        assert p.get("buttons"), "кнопки не доехали до исполнителя — экран обещал их зря"
        assert p.get("silent") is True, "тихая отправка не доехала"
        assert o["scheduled_for"] is not None, "отсрочка не доехала: A/B ушёл бы сразу"


def test_a_refusal_is_written_in_russian():
    """Отказ движка владелец читает как есть — значит, по-русски."""
    import asyncpg

    from services.broadcaster import ab_test_broadcast

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool, sleepers=0, fresh=0)
            no_subs = await ab_test_broadcast(
                pool, OWNER, BOT,
                [{"text": "А", "weight": 1}, {"text": "Б", "weight": 1}])
            alien = await ab_test_broadcast(
                pool, OWNER + 1, BOT,
                [{"text": "А", "weight": 1}, {"text": "Б", "weight": 1}])
            one = await ab_test_broadcast(pool, OWNER, BOT, [])
            return no_subs, alien, one
        finally:
            await _clean(pool)
            await pool.close()

    for res in _run(_go()):
        assert res["ok"] is False
        err = res["error"]
        assert not any("a" <= ch.lower() <= "z" for ch in err), (
            f"отказ уедет владельцу по-английски: {err!r}")


def test_the_screen_has_the_door():
    """Движок без двери — это функция, о которой владелец не узнает."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "/api/miniapp/broadcast/ab_test" in html, (
        "у A/B-рассылки снова нет двери на экране")
    assert "bcastTextB" in html, "нет поля второго варианта текста"
    assert "variants:" in html, "экран не отправляет варианты"


def test_the_segment_condition_lives_in_one_place():
    """Два списка условий сегмента = «Спящим» значит разное в разных местах."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    worker = (root / "services" / "op_worker.py").read_text(encoding="utf-8")
    assert "interval '14 days'" not in worker, (
        "условие сегмента снова списано в исполнитель — оно обязано жить в "
        "broadcaster.BROADCAST_SEGMENTS")
