"""JIT Postgres выключен у соединений пула — и остаётся выключенным.

Зачем. JIT включается сам, когда оценка стоимости запроса выше
`jit_above_cost` (100 000), а оценки у агрегатов продукта завышены на порядки:
список ботов с подсчётом подписчиков оценивается в 200 000 при настоящих
37 000 строк. Компиляция стоит 20–150 мс на КАЖДЫЙ вызов — кэша
скомпилированного кода у Postgres нет. Замер на стенде (1,5 млн подписчиков,
400 тыс. операций): «Итоги» на главной 187 → 33 мс, «Новые пользователи»
471 → 349 мс, «Аналитика» 146 → 123 мс.

Ловушки, которые здесь стерегутся:
- параметр задаётся ПОДКЛЮЧЕНИЕМ, а не `SET`: пул чистит «грязное»
  соединение `RESET ALL` (database/lean_reset.py), и `SET` бы отменился, а
  параметр подключения переживает сброс;
- пулер, не принимающий незнакомый параметр, не должен валить старт продукта:
  `create_pool` повторяет подключение без него.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_pool_config_turns_jit_off_by_default(monkeypatch):
    from database.pool_config import get_pool_config

    monkeypatch.delenv("DB_JIT", raising=False)
    assert get_pool_config()["server_settings"] == {"jit": "off"}


@pytest.mark.parametrize("raw,want", [("on", {"jit": "on"}), (" OFF ", {"jit": "off"}),
                                      ("", {}), ("мусор", {})])
def test_db_jit_env_is_honoured(monkeypatch, raw, want):
    from database.pool_config import get_pool_config

    monkeypatch.setenv("DB_JIT", raw)
    assert get_pool_config()["server_settings"] == want


def test_create_pool_retries_without_session_parameters(monkeypatch):
    """Пулер отбил параметр подключения — продукт всё равно стартует."""
    import asyncpg

    from database import db

    calls = []

    class _Stop(Exception):
        pass

    async def fake_create_pool(dsn, **kw):
        calls.append(dict(kw))
        if "server_settings" in kw:
            raise asyncpg.exceptions.ProtocolViolationError(
                "unsupported startup parameter: jit")
        raise _Stop  # дальше create_pool нам не интересен

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.delenv("DB_JIT", raising=False)
    with pytest.raises(_Stop):
        asyncio.new_event_loop().run_until_complete(db.create_pool())
    assert len(calls) == 2
    assert calls[0]["server_settings"] == {"jit": "off"}
    assert "server_settings" not in calls[1]


def test_create_pool_does_not_retry_when_nothing_to_drop(monkeypatch):
    """Без параметров сессии повторять нечего — настоящая ошибка наружу."""
    from database import db

    calls = []

    async def fake_create_pool(dsn, **kw):
        calls.append(kw)
        raise ConnectionRefusedError("база лежит")

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setenv("DB_JIT", "")
    with pytest.raises(ConnectionRefusedError):
        asyncio.new_event_loop().run_until_complete(db.create_pool())
    assert len(calls) == 1


def test_jit_stays_off_after_the_pool_resets_a_dirty_connection(monkeypatch):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    from database.lean_reset import LeanResetConnection
    from database.pool_config import get_pool_config

    monkeypatch.delenv("DB_JIT", raising=False)
    cfg = get_pool_config()

    async def _go():
        try:
            pool = await asyncpg.create_pool(
                DSN, connection_class=LeanResetConnection, min_size=1, max_size=1,
                server_settings=cfg["server_settings"])
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            assert await pool.fetchval("SHOW jit") == "off"
            # «Грязное» соединение: SET без LOCAL — пул сделает RESET ALL.
            async with pool.acquire() as conn:
                await conn.execute("SET jit = on")
                assert await conn.fetchval("SHOW jit") == "on"
            assert await pool.fetchval("SHOW jit") == "off", (
                "после RESET ALL пула JIT снова включился")
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(_go())
