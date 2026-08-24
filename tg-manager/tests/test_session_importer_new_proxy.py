"""Задать НОВЫЙ прокси прямо в форме импорта — он создаётся и СРАЗУ закрепляется.

Раньше при импорте сессий сырой proxy_url использовался ТОЛЬКО для валидации,
а закреплялся лишь ранее сохранённый proxy_id. Значит вставить новый прокси
«перед подключением» было нельзя — приходилось сначала идти в раздел прокси.
Теперь: proxy_id не задан, но есть валидный proxy_url → создаём user_proxy
владельца (тот же шифр+fingerprint-дедуп, что и «Добавить прокси») и закрепляем
его за импортируемыми аккаунтами.
"""
from __future__ import annotations

from unittest.mock import patch

import asyncpg
import pytest

from services import session_importer

_LINE = "1" + "A" * 130
_VALID = {"valid": True, "phone": "+79001112233", "user_id": 1,
          "first_name": "T", "username": "t"}


class _DispatchPool:
    """Fake-pool с маршрутизацией по тексту запроса — под многошаговый импорт."""

    def __init__(self, *, undefined_fp=False):
        self.undefined_fp = undefined_fp
        self.inserted_account_args = None
        self.proxy_insert_seen = False

    async def fetchval(self, q, *a):
        # проверка владельца proxy_id — в этом сценарии не вызывается (proxy_id=None)
        return None

    async def fetchrow(self, q, *a):
        if "INSERT INTO user_proxies" in q:
            self.proxy_insert_seen = True
            if self.undefined_fp and "proxy_fp" in q:
                raise asyncpg.UndefinedColumnError("column proxy_fp does not exist")
            return {"id": 4242}
        if "FROM tg_accounts WHERE session_fp" in q:
            return None  # не дубль — продолжаем импорт
        return None

    async def execute(self, q, *a):
        if "INSERT INTO tg_accounts" in q:
            self.inserted_account_args = a
        return "INSERT 0 1"


def test_proxy_type_detects_schemes():
    assert session_importer._proxy_type("socks5://h:1") == "socks5"
    assert session_importer._proxy_type("SOCKS4://h:1") == "socks4"
    assert session_importer._proxy_type("http://h:1") == "http"
    assert session_importer._proxy_type("ftp://h:1") is None
    assert session_importer._proxy_type("") is None
    assert session_importer._proxy_type(None) is None


@pytest.mark.asyncio
async def test_ensure_user_proxy_creates_and_returns_id():
    pool = _DispatchPool()
    pid = await session_importer.ensure_user_proxy(pool, 5, "socks5://u:p@1.2.3.4:1080")
    assert pid == 4242
    assert pool.proxy_insert_seen


@pytest.mark.asyncio
async def test_ensure_user_proxy_rejects_non_proxy():
    pool = _DispatchPool()
    assert await session_importer.ensure_user_proxy(pool, 5, "not-a-proxy") is None
    assert await session_importer.ensure_user_proxy(pool, 5, "") is None
    assert not pool.proxy_insert_seen


@pytest.mark.asyncio
async def test_ensure_user_proxy_fallback_without_fp_column():
    # лаг миграции proxy_fp — вставка всё равно проходит фолбэком без колонки
    pool = _DispatchPool(undefined_fp=True)
    pid = await session_importer.ensure_user_proxy(pool, 5, "http://1.2.3.4:8080")
    assert pid == 4242


@pytest.mark.asyncio
async def test_import_binds_new_proxy_to_account():
    pool = _DispatchPool()
    with patch.object(session_importer, "validate_session", return_value=_VALID):
        res = await session_importer.import_sessions(
            pool, owner_id=5, raw_data=_LINE,
            proxy_url="socks5://u:p@9.9.9.9:1080", proxy_id=None)
    assert res["imported"] == 1
    assert pool.proxy_insert_seen, "новый прокси не был создан"
    # последний аргумент INSERT tg_accounts — proxy_id закреплённого прокси
    assert pool.inserted_account_args is not None
    assert pool.inserted_account_args[-1] == 4242, "новый proxy_id не закреплён за аккаунтом"


@pytest.mark.asyncio
async def test_import_no_proxy_stays_direct():
    # пусто/невалидный proxy_url → proxy_id остаётся None (прямой host-IP), не падаем
    pool = _DispatchPool()
    with patch.object(session_importer, "validate_session", return_value=_VALID):
        res = await session_importer.import_sessions(
            pool, owner_id=5, raw_data=_LINE, proxy_url=None, proxy_id=None)
    assert res["imported"] == 1
    assert not pool.proxy_insert_seen
    assert pool.inserted_account_args[-1] is None
