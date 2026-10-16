"""Отказанная рассылка не уходит на следующем деплое.

Немедленная рассылка создаётся в два шага: сперва строка в `broadcasts` (её
читает исполнитель и прогресс), потом постановка операции через шину. Второй
шаг имеет право ОТКАЗАТЬ — это его работа: гейт тарифа (`PlanRequiredError`) и
предохранитель Ban Weather (`ImmunityBlockedError`). Владелец получал честный
отказ, а строка оставалась в 'pending'.

Ровно статусы ('pending','running') поднимает старт процесса
(`broadcaster.resume_interrupted`). Рабочая ветка едет в прод на каждом пуше,
так что на ближайшем деплое запрещённая рассылка уходила ВСЕЙ аудитории бота:
без операции, без прогресса и мимо предохранителя, который её запретил. Иначе
говоря, шторм банов останавливал рассылку до следующего деплоя, а оплату
подписки можно было не делать вовсе.

Отложенная рассылка этого не знала с самого начала: там строка заранее не
создаётся, и в коде рядом написано, почему.

Нужен живой Postgres: проверяется, что строка реально вышла из выборки
воскрешения, а заглушка пула SQL не исполняет.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993429
BOT_ID = 880099773

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,'refused_test_bot',TRUE,$3)",
        f"1234:refused-test-{BOT_ID}", BOT_ID, OWNER))
    for uid in (7101, 7102):
        _run(p.execute(
            "INSERT INTO bot_users(bot_id, user_id, is_active) VALUES($1,$2,TRUE)",
            BOT_ID, uid))
    yield p
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


def _resurrectable_ids(pool) -> set:
    """Что поднимет старт процесса — тем же запросом, которым он это делает."""
    from database import db

    rows = _run(db.get_interrupted_broadcasts(pool))
    return {int(r["id"]) for r in rows}


def test_the_safety_brake_is_not_undone_by_the_next_restart(pool, monkeypatch):
    from services import broadcaster, operation_bus

    async def _refuse(*a, **kw):
        raise operation_bus.ImmunityBlockedError(
            "⛔️ Шторм банов: новые рассылки приостановлены")

    monkeypatch.setattr(operation_bus, "submit", _refuse)

    before = _resurrectable_ids(pool)
    res = _run(broadcaster.mass_broadcast_with_scheduling(
        pool, OWNER, BOT_ID, "текст запрещённой рассылки", {}))
    assert res.get("ok") is False, f"предохранитель не отказал: {res}"

    new_rows = _run(pool.fetch(
        "SELECT id, status FROM broadcasts WHERE bot_id=$1", BOT_ID))
    assert new_rows, "пробник сломан: запись рассылки не создавалась вовсе"
    leaked = {int(r["id"]) for r in new_rows} & (_resurrectable_ids(pool) - before)
    assert not leaked, (
        "запрещённая рассылка осталась в ('pending','running'): ближайший "
        "рестарт процесса поднимет её через resume_interrupted и отправит всей "
        "аудитории бота — мимо предохранителя, который её запретил")
    assert all(str(r["status"]) == "cancelled" for r in new_rows), (
        [dict(r) for r in new_rows])


def test_the_door_does_not_touch_a_live_broadcast(pool):
    """Дверь снимает только то, что ещё не начинало отправку."""
    from services import broadcaster

    running = int(_run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, "
        "                       sent_count) "
        "VALUES($1,'идёт',10,'running',$2,4) RETURNING id", BOT_ID, OWNER)))
    started = int(_run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, "
        "                       sent_count) "
        "VALUES($1,'начатая',10,'pending',$2,3) RETURNING id", BOT_ID, OWNER)))
    _run(broadcaster.abandon_unstarted(pool, running))
    _run(broadcaster.abandon_unstarted(pool, started))
    assert _run(pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", running)) == "running"
    assert _run(pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", started)) == "pending", (
        "дверь сняла рассылку, которая уже отправила часть сообщений")


def test_the_mini_app_handler_abandons_the_row_when_the_bus_refuses():
    """Ручка мини-аппа — второй вход в тот же двухшаговый путь.

    Её тело проверяем разбором исходника (так же, как соседние проверки ручек):
    поднимать aiohttp-приложение ради одной ветки отказа дороже, чем пользы.
    Поведение самой двери проверено выше на живой базе.
    """
    from tests.miniapp_source import api_function

    seg = api_function("create_broadcast")
    tree = ast.parse("async def _wrap():\n" + "\n".join(
        "    " + ln for ln in seg.splitlines()))
    submits = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "submit"]
    assert submits, "пробник сломан: постановки операции в ручке не нашлось"

    guarded = False
    for tr in ast.walk(tree):
        if not isinstance(tr, ast.Try):
            continue
        body_submits = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "submit" for n in ast.walk(ast.Module(body=tr.body, type_ignores=[]))
        )
        if not body_submits:
            continue
        for h in tr.handlers:
            if any("abandon_unstarted" in (getattr(n, "attr", "") or getattr(n, "id", "") or "")
                   for n in ast.walk(h)):
                guarded = True
    assert guarded, (
        "отказ шины в ручке мини-аппа оставляет созданную запись рассылки "
        "«ожидающей» — старт процесса отправит её всей аудитории мимо отказа")
