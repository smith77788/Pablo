"""Регрессия: контактная книга (services/contacts_hub) была полностью
нерабочей.

sync_service.sync_account импортировал get_account_for_telethon из неверного
модуля (services.account_manager вместо database.db) и вызывал
client.get_contacts() — метода, которого в Telethon TelegramClient не
существует. Любая синхронизация падала мгновенно.

search_engine._build_search_conditions (2+ слова) и однословный путь
search_contacts (1 слово) оба обращались к несуществующей скалярной колонке
`email` у unified_contacts (в схеме есть только emails JSONB) — любой поиск
контактов падал с `column "email" does not exist`.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from services.contacts_hub import search_engine, sync_service


class _FakePool:
    """Minimal asyncpg pool mock: no existing contact → insert path."""

    def __init__(self):
        self.executed = []

    async def fetchrow(self, query, *args):
        return None  # no existing contact -> insert branch

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 1"


def test_sync_account_imports_get_account_for_telethon_from_database_db():
    src = inspect.getsource(sync_service.sync_account)
    assert "from database.db import get_account_for_telethon" in src, (
        "get_account_for_telethon живёт в database.db, а не в services.account_manager"
    )


def test_sync_account_uses_real_account_manager_get_contacts():
    src = inspect.getsource(sync_service.sync_account)
    assert "account_manager.get_contacts(" in src, (
        "должен использовать реальный account_manager.get_contacts, "
        "а не несуществующий client.get_contacts()"
    )
    assert "await client.get_contacts()" not in src


def test_sync_account_duration_is_not_always_zero():
    src = inspect.getsource(sync_service.sync_account)
    assert "time.monotonic() - started" in src, (
        "duration_ms должен считаться от started, а не (time.time() - time.time())"
    )


def test_search_engine_never_references_scalar_email_column():
    src = inspect.getsource(search_engine)
    assert "email ILIKE" not in src, (
        "unified_contacts не имеет скалярной колонки email (только emails JSONB) — "
        "любой запрос с этим условием падает"
    )


@pytest.mark.asyncio
async def test_sync_account_end_to_end_creates_contact():
    """Раньше падало на импорте/несуществующем методе раньше, чем доходило сюда."""
    pool = _FakePool()
    fake_acc = {"id": 7, "session_str": "abc", "owner_id": 1}
    fake_contacts = [
        {"user_id": 555, "username": "ivan", "phone": "+123",
         "first_name": "Ivan", "last_name": "Ivanov", "is_mutual": True}
    ]
    with patch("database.db.get_account_for_telethon", new=AsyncMock(return_value=fake_acc)), \
         patch("services.account_manager.get_contacts", new=AsyncMock(return_value=fake_contacts)), \
         patch("services.contacts_hub.repository.log_sync", new=AsyncMock()):
        result = await sync_service.sync_account(pool, owner_id=1, account_id=7)

    assert result["synced"] == 1
    assert result["created"] == 1
    assert "error" not in result
    insert_queries = [q for q, _ in pool.executed if "INSERT INTO unified_contacts" in q]
    assert insert_queries, "должен был вставить новый контакт"


@pytest.mark.asyncio
async def test_sync_account_propagates_is_premium():
    """is_premium из account_manager.get_contacts должен доходить до INSERT,
    а не жёстко зашиваться в False (иначе Telegram Premium-статус теряется)."""
    pool = _FakePool()
    fake_acc = {"id": 7, "session_str": "abc", "owner_id": 1}
    fake_contacts = [
        {"user_id": 555, "username": "ivan", "phone": "+123",
         "first_name": "Ivan", "last_name": "Ivanov", "is_mutual": True,
         "is_premium": True}
    ]
    with patch("database.db.get_account_for_telethon", new=AsyncMock(return_value=fake_acc)), \
         patch("services.account_manager.get_contacts", new=AsyncMock(return_value=fake_contacts)), \
         patch("services.contacts_hub.repository.log_sync", new=AsyncMock()):
        await sync_service.sync_account(pool, owner_id=1, account_id=7)

    insert = next(((q, a) for q, a in pool.executed if "INSERT INTO unified_contacts" in q), None)
    assert insert is not None
    # is_premium — 9-й позиционный аргумент INSERT ($9)
    assert insert[1][8] is True, "is_premium=True должен был пройти в INSERT"


class _ListPool:
    """Pool-мок для sync_all_accounts: возвращает список аккаунтов из fetch."""

    def __init__(self, account_ids, total_accounts=None):
        self._ids = account_ids
        self._total = total_accounts if total_accounts is not None else len(account_ids)

    async def fetch(self, query, *args):
        return [{"id": i, "phone": f"+{i}", "first_name": f"acc{i}",
                 "is_active": True} for i in self._ids]

    async def fetchval(self, query, *args):
        return self._total


@pytest.mark.asyncio
async def test_sync_all_accounts_no_accounts_returns_diagnostic():
    """0 аккаунтов → явное сообщение вместо немого нуля (частая причина
    жалобы «контакты не синхронизируются»)."""
    pool = _ListPool([])
    result = await sync_service.sync_all_accounts(pool, owner_id=1)
    assert result["accounts_found"] == 0
    assert result["total_synced"] == 0
    assert result.get("message"), "должна быть подсказка про подключение аккаунта"


@pytest.mark.asyncio
async def test_sync_all_accounts_all_failed_returns_diagnostic():
    """Аккаунты есть, но контактов 0 и есть ошибки → понятное сообщение
    про сессии/прокси, а не молчаливый ноль."""
    pool = _ListPool([10, 11])
    with patch.object(sync_service, "sync_account",
                      new=AsyncMock(return_value={"error": "timeout", "synced": 0})):
        result = await sync_service.sync_all_accounts(pool, owner_id=1)
    assert result["accounts_found"] == 2
    assert result["total_synced"] == 0
    assert result["errors"], "ошибки должны прокидываться наверх"
    assert result.get("message"), "должна быть подсказка проверить аккаунты"


@pytest.mark.asyncio
async def test_sync_all_accounts_runs_bounded_parallel():
    """Синхронизация аккаунтов должна идти параллельно (asyncio.gather), а не
    строго последовательно — иначе запрос упирается в таймаут шлюза."""
    src = inspect.getsource(sync_service.sync_all_accounts)
    assert "asyncio.gather" in src, "sync_all_accounts должен использовать gather"
    assert "Semaphore" in src, "должна быть ограниченная конкурентность (Semaphore)"


@pytest.mark.asyncio
async def test_sync_all_accounts_success_has_no_error_message():
    pool = _ListPool([10])
    with patch.object(sync_service, "sync_account",
                      new=AsyncMock(return_value={"synced": 3, "created": 3, "updated": 0})):
        result = await sync_service.sync_all_accounts(pool, owner_id=1)
    assert result["total_synced"] == 3
    assert result.get("message") is None


@pytest.mark.asyncio
async def test_sync_all_accounts_returns_per_account_details():
    """Ответ должен содержать пер-аккаунт детализацию, чтобы пользователь видел,
    что произошло с каждым аккаунтом (а не только агрегат)."""
    pool = _ListPool([10, 11])

    async def _fake(pool_, owner_id, acc_id):
        if acc_id == 10:
            return {"synced": 5, "created": 5, "updated": 0}
        return {"error": "session expired", "synced": 0}

    with patch.object(sync_service, "sync_account", new=_fake):
        result = await sync_service.sync_all_accounts(pool, owner_id=1)
    details = result["details"]
    assert len(details) == 2
    ok = [d for d in details if d["ok"]]
    bad = [d for d in details if not d["ok"]]
    assert ok and ok[0]["synced"] == 5
    assert bad and "session expired" in bad[0]["reason"]


@pytest.mark.asyncio
async def test_sync_all_accounts_includes_inactive_accounts():
    """Фильтр is_active=TRUE убран — деактивированные health-проверкой аккаунты
    с валидной сессией тоже должны синхронизироваться (частая причина «0»)."""
    src = inspect.getsource(sync_service.sync_all_accounts)
    # В самом SELECT-запросе не должно быть фильтрации по is_active (в комментарии —
    # можно). Проверяем строки с FROM tg_accounts.
    query_lines = [ln for ln in src.splitlines()
                   if "tg_accounts" in ln or "session_str IS NOT NULL" in ln]
    joined = " ".join(query_lines)
    assert "is_active=TRUE" not in joined and "is_active = TRUE" not in joined, (
        "sync не должен отсекать аккаунты по is_active в SELECT — сессия "
        "деактивированного аккаунта всё ещё валидна для чтения контактов"
    )
    assert "session_str IS NOT NULL" in src
