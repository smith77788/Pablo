"""Регресс: database.db.safe_count устойчив к миграционному лагу.

Сегментные рассылки строят COUNT со ссылкой на last_seen; если колонка ещё
не создана (schema lag), сырой fetchval падает UndefinedColumn и роняет всю
рассылку. safe_count обязан вернуть 0, а не пробросить исключение — тогда
mass_broadcast_with_scheduling завершается корректно, а не 500-ит.
"""
from __future__ import annotations

import pytest

from database import db


class _OkPool:
    async def fetchval(self, query, *args):
        return 137


class _BrokenPool:
    async def fetchval(self, query, *args):
        raise Exception('column "last_seen" does not exist')


class _NonePool:
    async def fetchval(self, query, *args):
        return None


@pytest.mark.asyncio
async def test_safe_count_returns_value_on_success():
    assert await db.safe_count(_OkPool(), "SELECT COUNT(*) FROM bot_users") == 137


@pytest.mark.asyncio
async def test_safe_count_swallows_migration_lag_error():
    # UndefinedColumn на отстающей схеме → 0, без проброса исключения
    assert await db.safe_count(_BrokenPool(), "SELECT COUNT(*) FROM x WHERE last_seen>now()") == 0


@pytest.mark.asyncio
async def test_safe_count_none_becomes_zero():
    assert await db.safe_count(_NonePool(), "SELECT COUNT(*) FROM empty") == 0
