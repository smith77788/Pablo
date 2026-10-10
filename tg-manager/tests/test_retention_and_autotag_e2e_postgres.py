"""Уборка базы и авторазметка сегментов — на НАСТОЯЩЕМ Postgres.

Зачем отдельно от остальных тестов: заглушка пула не проверяет ни типы
параметров, ни существование колонок. Список уборки (`_RETENTION`) — это
семнадцать таблиц и колонок, и опечатка в любой из них видна только там, где
запрос действительно исполняется: уборка ловит исключение, пишет строку в лог и
молча не чистит таблицу годами. То же с авторазметкой: её границы сегментов
живут в SQL (CASE по интервалам), и проверить, что «вчера» — это hot, а «сорок
дней назад» — lost, можно только в базе.

КАК ЗАПУСТИТЬ — как у остальных postgres-тестов, см. докстринг
tests/test_invite_e2e_postgres.py (нужен INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"
)

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        from database.db import SCHEMA_MIGRATIONS_DDL

        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
        )
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


_BOT_ID = 987654321
_OWNER = 5150


def _seed_bot(pool):
    async def _s():
        await pool.execute(
            """INSERT INTO managed_bots(token, bot_id, username, first_name, added_by)
               VALUES($1,$2,'e2e_bot','E2E',$3)
               ON CONFLICT (bot_id) DO NOTHING""",
            f"e2e-token-{_BOT_ID}", _BOT_ID, _OWNER,
        )
        await pool.execute("DELETE FROM user_tags WHERE bot_id=$1", _BOT_ID)
        await pool.execute("DELETE FROM user_activity WHERE bot_id=$1", _BOT_ID)

    _run(_s())


def test_уборка_проходит_по_живой_схеме(pool):
    """Каждый запрос уборки исполняется: ни одной таблицы или колонки не потеряно.

    `_record` кладёт -1 в результат, если запрос упал. На заглушке этого не
    увидеть: там падать нечему.
    """
    from services import db_maintenance

    results = _run(db_maintenance.run_once(pool))
    сломанные = sorted(k for k, v in results.items() if v == -1)
    assert not сломанные, (
        "запросы уборки не исполнились на живой схеме: " + ", ".join(сломанные)
    )
    # Каждая таблица из списка должна дать свою запись в результате.
    for table, _, _ in db_maintenance._RETENTION:
        assert table in results, f"таблица {table} не попала в отчёт уборки"


def test_границы_сегментов_на_живой_базе(pool):
    """hot / warm / cold / lost расставляются по тем же границам, что и раньше."""
    from database import db

    _seed_bot(pool)

    async def _seed_activity():
        строки = [
            (101, "12 hours"),    # hot   (<1 дня)
            (102, "3 days"),      # warm  (1-7 дней)
            (103, "10 days"),     # cold  (7-30 дней)
            (104, "40 days"),     # lost  (>30 дней)
        ]
        for uid, ago in строки:
            # Интервал — литералом: asyncpg выводит тип параметра из запроса, и
            # строка в $3::INTERVAL падает «invalid input for query argument»
            # ещё до Postgres. Значения здесь свои, из кода теста.
            await pool.execute(
                f"""INSERT INTO user_activity(bot_id, user_id, message_count, last_seen, first_seen)
                    VALUES($1,$2,1, now() - INTERVAL '{ago}', now() - INTERVAL '{ago}')""",
                _BOT_ID, uid,
            )
        # Застрявший тег пользователя, которого в активности уже нет.
        await pool.execute(
            """INSERT INTO user_tags(bot_id, user_id, tag) VALUES($1,$2,'activity:hot')
               ON CONFLICT (bot_id,user_id,tag) DO NOTHING""",
            _BOT_ID, 999,
        )

    _run(_seed_activity())

    segs = _run(db.autotag_by_activity(pool, _BOT_ID))
    assert segs == {"hot": 1, "warm": 1, "cold": 1, "lost": 1, "total": 4}, segs

    rows = _run(pool.fetch(
        "SELECT user_id, tag FROM user_tags WHERE bot_id=$1 ORDER BY user_id", _BOT_ID))
    теги = {r["user_id"]: r["tag"] for r in rows}
    assert теги == {
        101: "activity:hot",
        102: "activity:warm",
        103: "activity:cold",
        104: "activity:lost",
    }, теги
    assert 999 not in теги, "застрявший тег ушедшего пользователя не снят"


def test_повторная_разметка_не_плодит_теги(pool):
    """Второй прогон оставляет по одному тегу на человека."""
    from database import db

    _seed_bot(pool)
    _run(pool.execute(
        """INSERT INTO user_activity(bot_id, user_id, message_count, last_seen, first_seen)
           VALUES($1,$2,1, now() - INTERVAL '2 days', now() - INTERVAL '2 days')""",
        _BOT_ID, 201))

    _run(db.autotag_by_activity(pool, _BOT_ID))
    _run(db.autotag_by_activity(pool, _BOT_ID))

    rows = _run(pool.fetch(
        "SELECT tag FROM user_tags WHERE bot_id=$1 AND user_id=$2", _BOT_ID, 201))
    assert [r["tag"] for r in rows] == ["activity:warm"], [r["tag"] for r in rows]


def test_смена_сегмента_снимает_прежний_тег(pool):
    """Человек, ушедший из hot в cold, не остаётся с двумя тегами."""
    from database import db

    _seed_bot(pool)
    _run(pool.execute(
        """INSERT INTO user_activity(bot_id, user_id, message_count, last_seen, first_seen)
           VALUES($1,$2,1, now(), now())""",
        _BOT_ID, 301))
    _run(db.autotag_by_activity(pool, _BOT_ID))

    _run(pool.execute(
        "UPDATE user_activity SET last_seen = now() - INTERVAL '10 days' "
        "WHERE bot_id=$1 AND user_id=$2", _BOT_ID, 301))
    _run(db.autotag_by_activity(pool, _BOT_ID))

    rows = _run(pool.fetch(
        "SELECT tag FROM user_tags WHERE bot_id=$1 AND user_id=$2", _BOT_ID, 301))
    assert [r["tag"] for r in rows] == ["activity:cold"], [r["tag"] for r in rows]


def test_аудит_отказа_действительно_доезжает_до_базы(pool):
    """`record_manual_action` глотает любое исключение — значит расхождение со
    схемой не видно нигде, кроме живой базы: отказы изоляции (чужой аккаунт,
    чужой канал, чужой бот) просто перестали бы записываться, и об этом никто
    не узнал бы. Здесь проверяем, что строка реально ложится и что колонки
    заполнены теми значениями, которые потом читает разбор инцидента.
    """
    from database import db

    _run(pool.execute("DELETE FROM operation_audit WHERE owner_id=$1", 424242))
    _run(db.record_manual_action(
        pool, 424242, "foreign_accounts_refused",
        target="accounts:7,8", result="refused", error_msg="чужие аккаунты"))

    rows = _run(pool.fetch(
        "SELECT action, target, result, error_msg, account_id, operation_id, occurred_at "
        "FROM operation_audit WHERE owner_id=$1", 424242))
    _run(pool.execute("DELETE FROM operation_audit WHERE owner_id=$1", 424242))

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["action"] == "foreign_accounts_refused"
    assert row["target"] == "accounts:7,8"
    assert row["result"] == "refused"
    assert row["error_msg"] == "чужие аккаунты"
    # account_id и operation_id для ручного действия пустые намеренно —
    # отказ произошёл до того, как появилась операция и был выбран аккаунт.
    assert row["account_id"] is None
    assert row["operation_id"] is None
    assert row["occurred_at"] is not None
