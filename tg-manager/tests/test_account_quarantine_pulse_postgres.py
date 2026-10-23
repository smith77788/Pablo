"""Риск-пульс аккаунта — на НАСТОЯЩЕМ Postgres.

`is_account_quarantined` — предохранитель перед каждым действием каждого
аккаунта, и он fail-open: любая ошибка означает «не карантин», то есть
операция пойдёт на аккаунт, который на самом деле под ограничением. Сломанный
запрос здесь не виден ни в одном тесте с заглушкой пула — заглушка не проверяет
ни колонки, ни типы параметров, а исключение функция глотает по замыслу.
Поэтому и поведение, и план запроса проверяем на живой базе.

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

_OWNER = 777_230
_ACCOUNTS = 500
_EVENTS = 20_000

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def db():
    """Свой владелец, пятьсот аккаунтов и двадцать тысяч событий.

    Объём нужен именно для проверки плана: на пустой таблице планировщик
    выберет перебор независимо от индексов, и проверка ничего не докажет.
    """
    import asyncpg

    try:
        conn = _run(asyncpg.connect(DSN))
    except Exception as exc:
        pytest.skip(f"нет Postgres по INFRAGRAM_TEST_DSN: {exc}")

    async def _seed():
        await conn.execute("DELETE FROM restriction_events WHERE owner_id=$1", _OWNER)
        await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", _OWNER)
        ids = await conn.fetch(
            """INSERT INTO tg_accounts(owner_id, phone, session_str, is_active)
               SELECT $1, '+7900'||i, 'x', TRUE FROM generate_series(1,$2) i
               RETURNING id""", _OWNER, _ACCOUNTS)
        first = ids[0]["id"]
        # События размазаны по всем аккаунтам и по восьмидесяти дням: так же,
        # как в проде, где на один аккаунт приходится малая доля журнала.
        await conn.execute(
            """INSERT INTO restriction_events(owner_id, account_id, event_type, severity, created_at)
               SELECT $1, $2::bigint + (i % $3), 'flood_wait', 'info',
                      now() - (random() * INTERVAL '80 days')
                 FROM generate_series(1, $4) i""",
            _OWNER, first, _ACCOUNTS, _EVENTS)
        await conn.execute("ANALYZE restriction_events")
        return first

    first = _run(_seed())
    yield conn, first

    async def _cleanup():
        await conn.execute("DELETE FROM restriction_events WHERE owner_id=$1", _OWNER)
        await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", _OWNER)
        await conn.close()

    _run(_cleanup())


def test_индекс_по_аккаунту_существует(db):
    """Миграция schema_v230 доехала: индекс ведёт по account_id."""
    conn, _ = db
    defs = [r["indexdef"] for r in _run(conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='restriction_events'"))]
    assert [d for d in defs if "(account_id" in d.replace(" (", "(")], defs


def test_проверка_карантина_идёт_индексом(db):
    """План проверки карантина не читает журнал всей платформы за окно."""
    conn, acc_id = db
    plan = _run(conn.fetch(
        "EXPLAIN SELECT COUNT(*) FROM restriction_events re "
        "JOIN tg_accounts a ON a.id = re.account_id "
        "WHERE re.account_id=$1 "
        "AND re.created_at > NOW() - make_interval(days => $2) "
        "AND (a.risk_cleared_at IS NULL OR re.created_at > a.risk_cleared_at) "
        "AND (re.severity='critical' OR re.event_type ILIKE '%ban%')",
        acc_id, 3))
    text = "\n".join(r["QUERY PLAN"] for r in plan)
    assert "idx_restriction_events_account_created" in text, text


def test_свежее_критичное_ограничение_уводит_в_карантин(db):
    """Предохранитель срабатывает — это его единственная задача."""
    from services import infra_memory

    conn, acc_id = db
    assert _run(infra_memory.is_account_quarantined(conn, acc_id)) is False

    _run(conn.execute(
        """INSERT INTO restriction_events(owner_id, account_id, event_type, severity, created_at)
           VALUES($1,$2,'account_banned','critical', now() - INTERVAL '1 hour')""",
        _OWNER, acc_id))
    try:
        assert _run(infra_memory.is_account_quarantined(conn, acc_id)) is True
    finally:
        _run(conn.execute(
            "DELETE FROM restriction_events WHERE account_id=$1 AND severity='critical'",
            acc_id))


def test_старое_ограничение_карантин_не_держит(db):
    """За окном трёх суток событие уже не считается."""
    from services import infra_memory

    conn, acc_id = db
    _run(conn.execute(
        """INSERT INTO restriction_events(owner_id, account_id, event_type, severity, created_at)
           VALUES($1,$2,'account_banned','critical', now() - INTERVAL '10 days')""",
        _OWNER, acc_id))
    try:
        assert _run(infra_memory.is_account_quarantined(conn, acc_id)) is False
    finally:
        _run(conn.execute(
            "DELETE FROM restriction_events WHERE account_id=$1 AND severity='critical'",
            acc_id))


def test_ручной_сброс_риска_снимает_карантин(db):
    """`risk_cleared_at` — кнопка «взять в работу»: события до неё не считаются."""
    from services import infra_memory

    conn, acc_id = db
    _run(conn.execute(
        """INSERT INTO restriction_events(owner_id, account_id, event_type, severity, created_at)
           VALUES($1,$2,'account_banned','critical', now() - INTERVAL '2 hours')""",
        _OWNER, acc_id))
    _run(conn.execute(
        "UPDATE tg_accounts SET risk_cleared_at = now() - INTERVAL '1 hour' WHERE id=$1",
        acc_id))
    try:
        assert _run(infra_memory.is_account_quarantined(conn, acc_id)) is False
    finally:
        _run(conn.execute(
            "DELETE FROM restriction_events WHERE account_id=$1 AND severity='critical'",
            acc_id))
        _run(conn.execute(
            "UPDATE tg_accounts SET risk_cleared_at = NULL WHERE id=$1", acc_id))
