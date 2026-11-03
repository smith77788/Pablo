"""Сбой синхронизации копии плана не отменяет оплаченную подписку.

ЧТО БЫЛО. `billing.activate_subscription` после записи подписки обновляет
денормализованную копию `platform_users.current_plan` и «проглатывает» её
сбой: копия вторична. Но подтверждение оплаты (Stars, продление) зовёт её с
СОЕДИНЕНИЕМ внутри транзакции, а в Postgres упавший запрос ломает всю
транзакцию: следующий же запрос (доля рефереру) падал с
InFailedSQLTransactionError, и откатывалось само включение подписки за уже
списанные звёзды. Теперь копия пишется в точке сохранения.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

USER = 990417


def test_failed_copy_sync_inside_a_transaction_keeps_the_subscription():
    import asyncpg

    from services import billing

    async def go():
        try:
            conn = await asyncpg.connect(DSN)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:100]}")
        try:
            try:
                await conn.fetchval("SELECT 1 FROM subscriptions LIMIT 1")
                await conn.fetchval("SELECT 1 FROM platform_users LIMIT 1")
            except Exception as exc:
                pytest.skip(f"схема не накатана: {str(exc)[:80]}")
            await conn.execute("DELETE FROM subscriptions WHERE user_id=$1", USER)
            await conn.execute(
                """CREATE OR REPLACE FUNCTION _t_pu_fail() RETURNS trigger AS $$
                   BEGIN
                     IF NEW.user_id = 990417 THEN
                       RAISE EXCEPTION 'копия плана недоступна';
                     END IF;
                     RETURN NEW;
                   END $$ LANGUAGE plpgsql""")
            await conn.execute(
                "CREATE TRIGGER _t_pu_fail BEFORE UPDATE ON platform_users "
                "FOR EACH ROW EXECUTE FUNCTION _t_pu_fail()")
            await conn.execute(
                "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
                USER)
            try:
                async with conn.transaction():
                    expires = await billing.activate_subscription(
                        conn, USER, "paid", 1)
                    assert expires is not None
                    # Следующий запрос той же транзакции (так идёт доля рефереру).
                    await conn.fetchval("SELECT 1")
            finally:
                await conn.execute("DROP TRIGGER IF EXISTS _t_pu_fail ON platform_users")
                await conn.execute("DROP FUNCTION IF EXISTS _t_pu_fail()")
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id=$1 AND is_active", USER)
            assert n == 1, "оплаченная подписка откатилась из-за копии плана"
        finally:
            await conn.execute("DELETE FROM subscriptions WHERE user_id=$1", USER)
            await conn.execute("DELETE FROM platform_users WHERE user_id=$1", USER)
            await conn.close()

    asyncio.run(go())
