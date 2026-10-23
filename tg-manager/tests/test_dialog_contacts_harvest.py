"""Регрессия: система «не обнаруживала» контакты рабочих аккаунтов.

Жалоба пользователя дословно: «Не смотря на то что у каждого из подключенных
рабочих аккаунтов есть контакты — ваша система их не обнаруживает».

Причина: sync_account собирал контакты ТОЛЬКО из адресной книги
(account_manager.get_contacts → GetContactsRequest). Но у «рабочего» аккаунта
адресная книга часто пуста, а личных диалогов — десятки: собеседник в ЛС не
попадает в адресную книгу, пока его вручную не добавить в контакты. Конкуренты
(TeleRaptor и др.) собирают именно этих людей. Фикс: второй источник
account_manager.get_dialog_contacts + слияние по user_id (книга приоритетнее).

Эти тесты падают без фикса: без слияния диалогов sync_account видит только
адресную книгу, поэтому собеседник из ЛС (uid=900) не долетает до INSERT.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from services import account_manager
from services.contacts_hub import sync_service


class _FakePool:
    """Мок пула: контакта ещё нет → ветка INSERT; собираем все execute."""

    def __init__(self):
        self.executed = []

    async def fetchrow(self, query, *args):
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 1"


def _inserted_user_ids(pool):
    """user_id — 3-й позиционный аргумент INSERT unified_contacts ($3)."""
    out = []
    for q, a in pool.executed:
        if "INSERT INTO unified_contacts" in q:
            out.append(a[2])
    return out


@pytest.mark.asyncio
async def test_sync_merges_dialog_partners_into_contacts():
    """Собеседник из личного диалога, которого НЕТ в адресной книге, должен
    попасть в контакты. Без слияния диалогов он терялся — это и есть жалоба."""
    pool = _FakePool()
    fake_acc = {"id": 7, "session_str": "abc", "owner_id": 1}
    book = [
        {"user_id": 100, "username": "book_user", "phone": "+100",
         "first_name": "Из", "last_name": "Книги", "is_mutual": True},
    ]
    dialogs = [
        {"user_id": 900, "username": "dm_partner", "phone": "",
         "first_name": "Из", "last_name": "Диалога", "is_mutual": False,
         "source": "dialog"},
    ]
    with patch("database.db.get_account_for_telethon", new=AsyncMock(return_value=fake_acc)), \
         patch("services.account_manager.get_contacts", new=AsyncMock(return_value=book)), \
         patch("services.account_manager.get_dialog_contacts", new=AsyncMock(return_value=dialogs)), \
         patch("services.contacts_hub.repository.log_sync", new=AsyncMock()):
        result = await sync_service.sync_account(pool, owner_id=1, account_id=7)

    ids = _inserted_user_ids(pool)
    assert 900 in ids, "собеседник из ЛС должен долетать до контактов (это и есть жалоба)"
    assert 100 in ids, "контакт из адресной книги теряться не должен"
    assert result["synced"] == 2


@pytest.mark.asyncio
async def test_book_wins_over_dialog_for_same_user():
    """Один и тот же человек в книге и в диалоге = один контакт, и данные берём
    из книги (там is_mutual/телефон достовернее)."""
    pool = _FakePool()
    fake_acc = {"id": 7, "session_str": "abc", "owner_id": 1}
    book = [
        {"user_id": 222, "username": "petr", "phone": "+222",
         "first_name": "Пётр", "last_name": "", "is_mutual": True},
    ]
    dialogs = [
        {"user_id": 222, "username": "petr", "phone": "",
         "first_name": "Пётр", "last_name": "", "is_mutual": False,
         "source": "dialog"},
    ]
    with patch("database.db.get_account_for_telethon", new=AsyncMock(return_value=fake_acc)), \
         patch("services.account_manager.get_contacts", new=AsyncMock(return_value=book)), \
         patch("services.account_manager.get_dialog_contacts", new=AsyncMock(return_value=dialogs)), \
         patch("services.contacts_hub.repository.log_sync", new=AsyncMock()):
        result = await sync_service.sync_account(pool, owner_id=1, account_id=7)

    ids = _inserted_user_ids(pool)
    assert ids.count(222) == 1, "дубль по user_id недопустим"
    assert result["synced"] == 1
    insert = next(a for q, a in pool.executed if "INSERT INTO unified_contacts" in q)
    # is_mutual — 11-й позиционный аргумент ($11); книга (True) должна победить диалог (False)
    assert insert[10] is True, "данные должны браться из адресной книги, а не из диалога"


@pytest.mark.asyncio
async def test_dialog_fetch_failure_does_not_drop_book_contacts():
    """Сбой сбора диалогов (мёртвая сессия/таймаут на iter_dialogs) НЕ должен
    ронять уже полученную адресную книгу — иначе один хрупкий источник обнуляет
    другой рабочий."""
    pool = _FakePool()
    fake_acc = {"id": 7, "session_str": "abc", "owner_id": 1}
    book = [
        {"user_id": 100, "username": "book_user", "phone": "+100",
         "first_name": "Из", "last_name": "Книги", "is_mutual": True},
    ]
    boom = AsyncMock(side_effect=RuntimeError("iter_dialogs timeout"))
    with patch("database.db.get_account_for_telethon", new=AsyncMock(return_value=fake_acc)), \
         patch("services.account_manager.get_contacts", new=AsyncMock(return_value=book)), \
         patch("services.account_manager.get_dialog_contacts", new=boom), \
         patch("services.contacts_hub.repository.log_sync", new=AsyncMock()):
        result = await sync_service.sync_account(pool, owner_id=1, account_id=7)

    ids = _inserted_user_ids(pool)
    assert ids == [100], "адресная книга должна пережить сбой диалогов"
    assert result["synced"] == 1
    assert "error" not in result


def test_get_dialog_contacts_exists_and_returns_get_contacts_shape():
    """get_dialog_contacts должен существовать и отдавать тот же формат dict, что
    и get_contacts (иначе sync_account не сможет слить источники без спецкода)."""
    assert hasattr(account_manager, "get_dialog_contacts"), (
        "нужен второй источник контактов — собеседники из диалогов"
    )
    src = inspect.getsource(account_manager.get_dialog_contacts)
    # ключевые поля контракта, на которые опирается sync_account
    for key in ('"user_id"', '"username"', '"is_mutual"', '"phone"'):
        assert key in src, f"get_dialog_contacts должен отдавать {key}"
    # именно диалоги, а не адресная книга
    assert "iter_dialogs" in src, "источник — личные диалоги (iter_dialogs)"
    # боты/удалённые/сам аккаунт исключаются
    assert 'getattr(user, "bot"' in src and 'getattr(user, "deleted"' in src


def test_sync_account_actually_calls_dialog_source():
    """Статическая страховка: sync_account вызывает get_dialog_contacts —
    без этого вызова весь фикс мёртв, а тесты выше можно было бы обойти моком."""
    src = inspect.getsource(sync_service.sync_account)
    assert "account_manager.get_dialog_contacts(" in src, (
        "sync_account должен собирать и диалоги, а не только адресную книгу"
    )
