"""Регрессия: синхронизация контактов падала с ImportError.

`services/contacts_hub/sync_service.sync_account` импортировал
`get_account_for_telethon` из `services.account_manager`, где этой функции нет
(она живёт в `database.db`). На экране «CRM — Контакты» это давало баннер
«cannot import name 'get_account_for_telethon' from 'services.account_manager'»,
и синхронизация не работала.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from services.contacts_hub import sync_service


def test_names_resolve_from_correct_modules():
    from database.db import get_account_for_telethon  # noqa: F401
    from services.account_manager import _make_client  # noqa: F401


def test_sync_account_imports_from_database_db_not_account_manager():
    src = inspect.getsource(sync_service.sync_account)
    assert "from services.account_manager import _make_client, get_account_for_telethon" not in src, (
        "get_account_for_telethon не экспортируется из account_manager — ImportError"
    )
    assert "from database.db import get_account_for_telethon" in src, (
        "get_account_for_telethon должен импортироваться из database.db"
    )


def test_sync_account_passes_owner_id_for_scope():
    src = inspect.getsource(sync_service.sync_account)
    assert "get_account_for_telethon(pool, account_id, owner_id)" in src, (
        "sync_account должен скоупить выборку аккаунта по owner_id"
    )


def test_module_parses():
    ast.parse(Path(sync_service.__file__).read_text(encoding="utf-8"))
