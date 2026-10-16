"""Уборка журнала доставки рассылок — на НАСТОЯЩЕМ Postgres.

Запрос уборки связывает список идентификаторов как `$1::bigint[]` и считает
срок литеральным интервалом. Заглушка пула не проверяет ни типы параметров, ни
имена колонок, а уборка ловит исключение и пишет строку в лог — ровно так в
этом продукте уже один раз НИ РАЗУ не отработала чистка завершённых операций.
Поэтому сам отбор и удаление проверяем в базе.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py (нужен
INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"
)

_OWNER = 999_240
_BOT = 999_241
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    # asyncpg.create_pool берёт текущийループ через get_event_loop, а не тот, в
    # котором его ждут: без set_event_loop пул оказывается на чужом цикле и
    # первый же запрос падает «attached to a different loop».
    asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture()
def pool():
    import asyncpg

    async def _boot():
        # Пул создаём ВНУТРИ корутины: asyncpg привязывает его к циклу в момент
        # конструирования, и созданный снаружи пул оказался бы на чужом.
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"нет Postgres по INFRAGRAM_TEST_DSN: {exc}")

    _run(_подготовить(p))
    yield p
    _run(_очистить(p))
    _run(p.close())


async def _подготовить(p):
    """Чистый лист плюс бот-владелец: у broadcasts внешний ключ на managed_bots."""
    await _очистить(p)
    await p.execute(
        "INSERT INTO managed_bots(token, bot_id, added_by, is_active) "
        "VALUES($1,$2,$3,TRUE) ON CONFLICT (bot_id) DO NOTHING",
        f"{_BOT}:тест", _BOT, _OWNER)


async def _очистить(p):
    await p.execute(
        "DELETE FROM broadcast_delivery_log WHERE broadcast_id IN "
        "(SELECT id FROM broadcasts WHERE created_by=$1)", _OWNER)
    await p.execute("DELETE FROM broadcasts WHERE created_by=$1", _OWNER)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", _BOT)


async def _рассылка(p, *, status: str, возраст_дней: int, получателей: int) -> int:
    bc_id = await p.fetchval(
        """INSERT INTO broadcasts(bot_id, message_text, total_users, sent_count,
                                  status, created_by, created_at, finished_at)
           VALUES($1,'привет',$2,$2,$3,$4,
                  now() - make_interval(days => $5), now() - make_interval(days => $5))
           RETURNING id""",
        _BOT, получателей, status, _OWNER, возраст_дней)
    await p.execute(
        """INSERT INTO broadcast_delivery_log(broadcast_id, user_id)
           SELECT $1, i FROM generate_series(1, $2) i""", bc_id, получателей)
    return bc_id


def test_старая_завершённая_рассылка_теряет_журнал_но_получает_отметку(pool):
    from services import db_maintenance

    bc_id = _run(_рассылка(pool, status="done", возраст_дней=200, получателей=50))

    deleted, err = _run(db_maintenance._prune_broadcast_delivery_log(pool))
    assert err is None, err
    assert deleted >= 50, deleted

    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM broadcast_delivery_log WHERE broadcast_id=$1", bc_id)) == 0
    assert _run(pool.fetchval(
        "SELECT delivery_log_pruned FROM broadcasts WHERE id=$1", bc_id)) is True
    # Сводка по рассылке переживает уборку — отчёт остаётся.
    итог = _run(pool.fetchrow(
        "SELECT total_users, sent_count FROM broadcasts WHERE id=$1", bc_id))
    assert итог["total_users"] == 50 and итог["sent_count"] == 50


def test_свежая_рассылка_не_трогается(pool):
    from services import db_maintenance

    bc_id = _run(_рассылка(pool, status="done", возраст_дней=3, получателей=10))

    _run(db_maintenance._prune_broadcast_delivery_log(pool))

    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM broadcast_delivery_log WHERE broadcast_id=$1", bc_id)) == 10
    assert _run(pool.fetchval(
        "SELECT delivery_log_pruned FROM broadcasts WHERE id=$1", bc_id)) is False


def test_незавершённая_рассылка_не_трогается(pool):
    """Её журнал нужен для возобновления после падения — даже если она старая."""
    from services import db_maintenance

    bc_id = _run(_рассылка(pool, status="pending", возраст_дней=200, получателей=10))

    _run(db_maintenance._prune_broadcast_delivery_log(pool))

    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM broadcast_delivery_log WHERE broadcast_id=$1", bc_id)) == 10
    assert _run(pool.fetchval(
        "SELECT delivery_log_pruned FROM broadcasts WHERE id=$1", bc_id)) is False


def test_повторный_проход_уборки_не_повторяет_работу(pool):
    """Помеченная рассылка в отбор больше не попадает."""
    from services import db_maintenance

    _run(_рассылка(pool, status="done", возраст_дней=200, получателей=20))
    первый, _ = _run(db_maintenance._prune_broadcast_delivery_log(pool))
    второй, err = _run(db_maintenance._prune_broadcast_delivery_log(pool))
    assert первый >= 20, первый
    assert (второй, err) == (0, None)


def test_повтор_недоставленным_отказывает_на_живой_базе(pool):
    """Полный путь: уборка прошла — resend_undelivered отвечает отказом."""
    from services import broadcaster, db_maintenance

    bc_id = _run(_рассылка(pool, status="done", возраст_дней=200, получателей=5))
    _run(db_maintenance._prune_broadcast_delivery_log(pool))

    res = _run(broadcaster.resend_undelivered(pool, _OWNER, bc_id))
    assert res["ok"] is False and res["code"] == 409, res
