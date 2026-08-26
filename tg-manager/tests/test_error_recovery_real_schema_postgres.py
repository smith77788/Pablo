"""Автовосстановление после сбоя обязано что-то менять, а не падать молча.

ЧТО БЫЛО СЛОМАНО. Все три действия восстановления писали в несуществующие места:
  • `UPDATE operations …` — таблица называется operation_queue, а колонок
    `error`/`updated_at` в ней нет: зависшая операция не возвращалась в очередь
    ни разу и держала слот параллельности владельца до сторожа (60 минут);
  • `UPDATE tg_accounts SET needs_reauth = TRUE, updated_at = NOW()` — таких
    колонок нет: аккаунт с мёртвой сессией не помечался и продолжал разбирать
    задачи, падая на каждой;
  • `UPDATE user_proxies SET fail_count = fail_count + 1, last_error = …` — тоже
    нет: счётчик сбоев прокси не рос ни разу, и мёртвый прокси не выбывал.

Каждый запрос падал, `except` писал строчку в лог и возвращал False. Снаружи —
«само не чинится».

Заглушка пула такую ошибку не видит: SQL там не выполняется. Поэтому проверка на
живой базе. Запуск: INFRAGRAM_TEST_DSN=... pytest ...
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 771100
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    """Минимальные боевые определения трёх таблиц, которых касается модуль."""
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS rectest")
        finally:
            await setup.close()
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"search_path": "rectest"})
        await p.execute("""CREATE TABLE IF NOT EXISTS operation_queue (
                               id SERIAL PRIMARY KEY,
                               owner_id BIGINT NOT NULL,
                               op_type TEXT NOT NULL,
                               status TEXT NOT NULL DEFAULT 'pending',
                               error_msg TEXT,
                               last_error TEXT,
                               started_at TIMESTAMPTZ)""")
        await p.execute("""CREATE TABLE IF NOT EXISTS tg_accounts (
                               id SERIAL PRIMARY KEY,
                               owner_id BIGINT NOT NULL,
                               is_active BOOLEAN DEFAULT TRUE,
                               acc_status TEXT,
                               status_reason TEXT,
                               status_checked_at TIMESTAMPTZ)""")
        await p.execute("""CREATE TABLE IF NOT EXISTS user_proxies (
                               id SERIAL PRIMARY KEY,
                               owner_id BIGINT NOT NULL,
                               proxy_url TEXT NOT NULL,
                               is_alive BOOLEAN,
                               consecutive_failures INTEGER DEFAULT 0,
                               last_checked_at TIMESTAMPTZ)""")
        return p

    p = _run(_mk())
    yield p
    for t in ("operation_queue", "tg_accounts", "user_proxies"):
        _run(p.execute(f"DROP TABLE IF EXISTS {t} CASCADE"))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    for t in ("operation_queue", "tg_accounts", "user_proxies"):
        _run(pool.execute(f"DELETE FROM {t}"))
    yield


def test_stuck_operation_returns_to_the_queue(pool):
    """Главное: операция реально возвращается в pending, а не «логируется»."""
    from services.error_recovery import OperationRecoveryAction

    op_id = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, error_msg, started_at) "
        "VALUES($1,'invite','running','таймаут', NOW()) RETURNING id", OWNER))

    ok = _run(OperationRecoveryAction().recover(
        asyncio.TimeoutError(), {"operation_id": op_id, "pool": pool}))
    assert ok is True, "восстановление отчиталось об отказе"

    row = _run(pool.fetchrow(
        "SELECT status, error_msg, started_at FROM operation_queue WHERE id=$1", op_id))
    assert row["status"] == "pending", (
        f"операция осталась в статусе {row['status']} — слот владельца занят "
        "до сторожа (60 минут), а снаружи это «ничего не происходит»")
    assert row["error_msg"] is None and row["started_at"] is None


def test_only_running_operations_are_reset(pool):
    """Завершённую операцию восстановление трогать не должно."""
    from services.error_recovery import OperationRecoveryAction

    op_id = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status) "
        "VALUES($1,'invite','done') RETURNING id", OWNER))
    _run(OperationRecoveryAction().recover(
        asyncio.TimeoutError(), {"operation_id": op_id, "pool": pool}))
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "done"


def test_dead_session_account_is_marked_and_disabled(pool):
    """Аккаунт с мёртвой сессией обязан выбыть, иначе он падает на каждой задаче."""
    from services.error_recovery import SessionRecoveryAction

    acc_id = _run(pool.fetchval(
        "INSERT INTO tg_accounts(owner_id) VALUES($1) RETURNING id", OWNER))
    ok = _run(SessionRecoveryAction().recover(
        Exception("session revoked"), {"account_id": acc_id, "pool": pool}))
    assert ok is True

    row = _run(pool.fetchrow(
        "SELECT acc_status, status_reason, is_active, status_checked_at "
        "FROM tg_accounts WHERE id=$1", acc_id))
    assert row["acc_status"] == "session_expired", (
        "аккаунт не помечен — он продолжит разбирать задачи и падать на каждой")
    assert row["is_active"] is False
    assert row["status_reason"] and "session" in row["status_reason"].lower(), (
        "причина обязана быть записана: без неё оператору нечего чинить")
    assert row["status_checked_at"] is not None


def test_failing_proxy_increments_the_counter_selection_reads(pool):
    """Счётчик обязан расти в той колонке, которую читает выбор прокси."""
    from services.error_recovery import ProxyRecoveryAction

    pid = _run(pool.fetchval(
        "INSERT INTO user_proxies(owner_id, proxy_url, is_alive, consecutive_failures) "
        "VALUES($1,'socks5://x',TRUE,2) RETURNING id", OWNER))
    ok = _run(ProxyRecoveryAction().recover(
        OSError("proxy refused"), {"proxy_id": pid, "pool": pool}))
    assert ok is True

    row = _run(pool.fetchrow(
        "SELECT consecutive_failures, is_alive, last_checked_at "
        "FROM user_proxies WHERE id=$1", pid))
    assert row["consecutive_failures"] == 3, (
        "счётчик сбоев не вырос — мёртвый прокси никогда не выбудет")
    assert row["is_alive"] is False
    assert row["last_checked_at"] is not None


def test_null_counter_does_not_break_the_increment(pool):
    """NULL в счётчике не должен превращать инкремент в NULL."""
    from services.error_recovery import ProxyRecoveryAction

    pid = _run(pool.fetchval(
        "INSERT INTO user_proxies(owner_id, proxy_url, consecutive_failures) "
        "VALUES($1,'socks5://y',NULL) RETURNING id", OWNER))
    _run(ProxyRecoveryAction().recover(
        OSError("proxy refused"), {"proxy_id": pid, "pool": pool}))
    assert _run(pool.fetchval(
        "SELECT consecutive_failures FROM user_proxies WHERE id=$1", pid)) == 1


def test_missing_context_is_a_clean_refusal(pool):
    """Без id восстанавливать нечего — это отказ, а не падение."""
    from services.error_recovery import (
        OperationRecoveryAction, ProxyRecoveryAction, SessionRecoveryAction)

    for action in (OperationRecoveryAction(), SessionRecoveryAction(),
                   ProxyRecoveryAction()):
        assert _run(action.recover(Exception("x"), {"pool": pool})) is False
