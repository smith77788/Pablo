"""Comprehensive tests for UCH API endpoints and service functions.

Covers: repository CRUD, search, sync, merge, export, trust, versioning,
CRM, smart tags, bulk ops, identity, relationships, AI assistant, stats.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakePool:
    """AsyncPG pool mock for comprehensive UCH testing."""

    def __init__(self, fetch_val=None, fetch_row=None, fetch_rows=None,
                 execute_val="UPDATE 1", error=None):
        self._fetch_val = fetch_val
        self._fetch_row = fetch_row
        self._fetch_rows = fetch_rows or []
        self._execute_val = execute_val
        self._error = error
        self._calls = []

    async def fetchval(self, query, *args):
        self._calls.append(("fetchval", query, args))
        if self._error:
            raise self._error
        return self._fetch_val

    async def fetchrow(self, query, *args):
        self._calls.append(("fetchrow", query, args))
        if self._error:
            raise self._error
        return self._fetch_row

    async def fetch(self, query, *args):
        self._calls.append(("fetch", query, args))
        if self._error:
            raise self._error
        return self._fetch_rows

    async def execute(self, query, *args):
        self._calls.append(("execute", query, args))
        if self._error:
            raise self._error
        return self._execute_val

    def acquire(self):
        return _FakeAcquire(self)

    async def release(self, conn):
        pass

    def transaction(self):
        return _FakeTransaction()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeAcquire:
    """Async context manager + awaitable for pool.acquire() patterns."""

    def __init__(self, pool: FakePool):
        self._pool = pool

    def __await__(self):
        async def _return_pool():
            return self._pool
        return _return_pool().__await__()

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, *args):
        pass


class _FakeTransaction:
    """Minimal async context manager for conn.transaction()."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Repository CRUD Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepositoryCRUD:
    """Tests for contacts_hub/repository.py CRUD operations."""

    @pytest.mark.asyncio
    async def test_get_contacts_empty(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        result = await get_contacts(pool, owner_id=123)
        assert result == {"contacts": [], "total": 0}

    @pytest.mark.asyncio
    async def test_get_contacts_with_results(self):
        from services.contacts_hub.repository import get_contacts
        rows = [
            {"id": "uuid-1", "first_name": "Ivan", "username": "ivan",
             "phones": "[]", "tags": "{}"},
            {"id": "uuid-2", "first_name": "Petr", "username": "petr",
             "phones": "[]", "tags": "{}"},
        ]
        pool = FakePool(fetch_rows=rows, fetch_val=2)
        result = await get_contacts(pool, owner_id=123)
        assert len(result["contacts"]) == 2
        assert result["total"] == 2
        assert result["contacts"][0]["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_get_contacts_with_search(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, search="ivan")
        last_call = pool._calls[-1]
        assert "ILIKE" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_tag_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, tag="vip")
        last_call = pool._calls[-1]
        assert "ANY(tags)" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_premium_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, premium_only=True)
        last_call = pool._calls[-1]
        assert "is_premium" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_favorite_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, favorite_only=True)
        last_call = pool._calls[-1]
        assert "is_favorite" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_multi_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, multi_only=True)
        last_call = pool._calls[-1]
        assert "contact_sources" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_account_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, account_id=5)
        last_call = pool._calls[-1]
        assert "contact_sources" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_group_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, group_id=10)
        last_call = pool._calls[-1]
        assert "contact_group_members" in last_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_sort_by_username(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, sort_by="username")
        first_call = pool._calls[0]
        assert "username" in first_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_sort_by_discovered(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, sort_by="discovered")
        first_call = pool._calls[0]
        assert "discovered_at" in first_call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_limit_offset(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, limit=50, offset=25)
        first_call = pool._calls[0]
        assert 50 in first_call[2]
        assert 25 in first_call[2]

    @pytest.mark.asyncio
    async def test_get_contact_detail(self):
        from services.contacts_hub.repository import get_contact
        row = {"id": "uuid-1", "first_name": "Ivan", "owner_id": 123}
        pool = FakePool(fetch_row=row, fetch_rows=[])
        result = await get_contact(pool, "uuid-1", 123)
        assert result is not None
        assert result["contact"]["first_name"] == "Ivan"
        assert "sources" in result
        assert "history" in result
        assert "groups" in result

    @pytest.mark.asyncio
    async def test_get_contact_not_found(self):
        from services.contacts_hub.repository import get_contact
        pool = FakePool(fetch_row=None)
        result = await get_contact(pool, "nonexistent", 123)
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_contact_new(self):
        from services.contacts_hub.repository import upsert_contact
        pool = FakePool()
        data = {
            "telegram_user_id": 12345,
            "username": "ivan",
            "first_name": "Ivan",
            "last_name": "Ivanov",
            "phones": ["+123456"],
            "is_premium": True,
        }
        result = await upsert_contact(pool, 123, data)
        assert result is not None
        assert len(result) > 0
        assert "INSERT" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_upsert_contact_with_existing_id(self):
        from services.contacts_hub.repository import upsert_contact
        pool = FakePool()
        data = {
            "id": "existing-uuid",
            "telegram_user_id": 12345,
            "username": "ivan",
        }
        result = await upsert_contact(pool, 123, data)
        assert result == "existing-uuid"

    @pytest.mark.asyncio
    async def test_update_contact_success(self):
        from services.contacts_hub.repository import update_contact
        pool = FakePool(execute_val="UPDATE 1")
        updates = {"first_name": "NewName", "notes": "Updated"}
        result = await update_contact(pool, "uuid-1", 123, updates)
        assert result is True
        assert "UPDATE" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_update_contact_not_found(self):
        from services.contacts_hub.repository import update_contact
        pool = FakePool(execute_val="UPDATE 0")
        result = await update_contact(pool, "uuid-1", 123, {"first_name": "X"})
        assert result is False

    @pytest.mark.asyncio
    async def test_update_contact_jsonb_fields(self):
        from services.contacts_hub.repository import update_contact
        pool = FakePool(execute_val="UPDATE 1")
        updates = {"phones": ["+111", "+222"], "emails": ["a@b.com"]}
        result = await update_contact(pool, "uuid-1", 123, updates)
        assert result is True
        call_args = pool._calls[0]
        assert "jsonb" in call_args[1]

    @pytest.mark.asyncio
    async def test_update_contact_tags_field(self):
        from services.contacts_hub.repository import update_contact
        pool = FakePool(execute_val="UPDATE 1")
        updates = {"tags": ["vip", "client"]}
        result = await update_contact(pool, "uuid-1", 123, updates)
        assert result is True
        call_args = pool._calls[0]
        assert "text[]" in call_args[1]

    @pytest.mark.asyncio
    async def test_delete_contact_success(self):
        from services.contacts_hub.repository import delete_contact
        pool = FakePool(execute_val="DELETE 1")
        result = await delete_contact(pool, "uuid-1", 123)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_contact_not_found(self):
        from services.contacts_hub.repository import delete_contact
        pool = FakePool(execute_val="DELETE 0")
        result = await delete_contact(pool, "uuid-1", 123)
        assert result is False

    @pytest.mark.asyncio
    async def test_get_contact_groups(self):
        from services.contacts_hub.repository import get_contact_groups
        pool = FakePool(fetch_rows=[
            {"id": 1, "name": "Clients", "member_count": 5},
            {"id": 2, "name": "VIP", "member_count": 3},
        ])
        result = await get_contact_groups(pool, 123)
        assert len(result) == 2
        assert result[0]["name"] == "Clients"
        assert result[1]["member_count"] == 3

    @pytest.mark.asyncio
    async def test_get_contact_groups_empty(self):
        from services.contacts_hub.repository import get_contact_groups
        pool = FakePool(fetch_rows=[])
        result = await get_contact_groups(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_add_contact_to_group_success(self):
        from services.contacts_hub.repository import add_contact_to_group
        pool = FakePool(execute_val="INSERT 0 1")
        result = await add_contact_to_group(pool, "uuid-1", 1)
        assert result is True

    @pytest.mark.asyncio
    async def test_add_contact_to_group_error(self):
        from services.contacts_hub.repository import add_contact_to_group
        pool = FakePool(error=Exception("FK violation"))
        result = await add_contact_to_group(pool, "uuid-1", 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_contact_from_group_success(self):
        from services.contacts_hub.repository import remove_contact_from_group
        pool = FakePool(execute_val="DELETE 1")
        result = await remove_contact_from_group(pool, "uuid-1", 1)
        assert result is True

    @pytest.mark.asyncio
    async def test_remove_contact_from_group_not_found(self):
        from services.contacts_hub.repository import remove_contact_from_group
        pool = FakePool(execute_val="DELETE 0")
        result = await remove_contact_from_group(pool, "uuid-1", 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_get_contact_stats(self):
        from services.contacts_hub.repository import get_contact_stats
        pool = FakePool(fetch_val=42, fetch_rows=[{"account_id": 1, "cnt": 10}])
        result = await get_contact_stats(pool, 123)
        assert result["total"] == 42
        assert "premium" in result
        assert "with_username" in result
        assert "by_account" in result

    @pytest.mark.asyncio
    async def test_log_contact_history(self):
        from services.contacts_hub.repository import log_contact_history
        pool = FakePool()
        await log_contact_history(
            pool, "uuid-1", 123, "edit", "first_name", "Old", "New", "user"
        )
        assert len(pool._calls) == 1
        assert "INSERT INTO contact_history" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_log_sync(self):
        from services.contacts_hub.repository import log_sync
        pool = FakePool()
        await log_sync(pool, 123, 5, "auto", 100, 50, 50, 0, 1500, None)
        assert len(pool._calls) == 1
        assert "INSERT INTO contact_sync_log" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_log_sync_with_error(self):
        from services.contacts_hub.repository import log_sync
        pool = FakePool()
        await log_sync(pool, 123, 5, "auto", 0, 0, 0, 0, 500, "Connection timeout")
        assert len(pool._calls) == 1
        assert "Connection timeout" in pool._calls[0][2]


# ═══════════════════════════════════════════════════════════════════════════════
# Search Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchEngine:
    """Tests for contacts_hub/search_engine.py"""

    def test_tokenize_normal(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("Ivan Ivanov") == ["ivan", "ivanov"]

    def test_tokenize_empty(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("") == []

    def test_tokenize_whitespace(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("   ") == []

    def test_tokenize_single_char(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("a") == []

    def test_tokenize_two_chars(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("ab") == ["ab"]

    def test_tokenize_cjk(self):
        from services.contacts_hub.search_engine import _tokenize
        tokens = _tokenize("ivanのdeveloper")
        assert len(tokens) >= 1

    def test_build_search_conditions_empty(self):
        from services.contacts_hub.search_engine import _build_search_conditions
        where, params = _build_search_conditions([], 3)
        assert where == ""
        assert params == []

    def test_build_search_conditions_single(self):
        from services.contacts_hub.search_engine import _build_search_conditions
        where, params = _build_search_conditions(["ivan"], 3)
        assert "ILIKE" in where
        assert params == ["%ivan%"]

    def test_build_search_conditions_multiple(self):
        from services.contacts_hub.search_engine import _build_search_conditions
        where, params = _build_search_conditions(["ivan", "petrov"], 3)
        assert "AND" in where
        assert len(params) == 2

    @pytest.mark.asyncio
    async def test_search_contacts_empty_query(self):
        from services.contacts_hub.search_engine import search_contacts
        pool = FakePool(fetch_rows=[])
        result = await search_contacts(pool, 123, "")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_contacts_single_token(self):
        from services.contacts_hub.search_engine import search_contacts
        pool = FakePool(fetch_rows=[{"id": "1", "first_name": "Ivan"}])
        result = await search_contacts(pool, 123, "ivan")
        assert len(result) == 1
        assert result[0]["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_search_contacts_multi_token(self):
        from services.contacts_hub.search_engine import search_contacts
        pool = FakePool(fetch_rows=[{"id": "1", "first_name": "Ivan"}])
        result = await search_contacts(pool, 123, "ivan petrov")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_search_contacts_with_limit(self):
        from services.contacts_hub.search_engine import search_contacts
        pool = FakePool(fetch_rows=[])
        await search_contacts(pool, 123, "test", limit=10)
        last_call = pool._calls[-1]
        assert 10 in last_call[2]

    @pytest.mark.asyncio
    async def test_search_contacts_spotlight_empty(self):
        from services.contacts_hub.search_engine import search_contacts_spotlight
        pool = FakePool(fetch_rows=[])
        result = await search_contacts_spotlight(pool, 123, "")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_contacts_spotlight_single(self):
        from services.contacts_hub.search_engine import search_contacts_spotlight
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Ivan", "last_name": "Petrov",
             "username": "ivanp", "company": "Acme", "tags": ["vip"]}
        ])
        result = await search_contacts_spotlight(pool, 123, "ivan")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_search_contacts_spotlight_multi_dedup(self):
        from services.contacts_hub.search_engine import search_contacts_spotlight
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Ivan", "last_name": "Petrov",
             "username": "ivan", "company": "", "tags": []},
            {"id": "1", "first_name": "Ivan", "last_name": "Petrov",
             "username": "ivan", "company": "", "tags": []},
        ])
        result = await search_contacts_spotlight(pool, 123, "ivan ivan")
        ids = [r["id"] for r in result]
        assert len(ids) == len(set(ids))


# ═══════════════════════════════════════════════════════════════════════════════
# Merge Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMergeEngine:
    """Tests for contacts_hub/merge_engine.py"""

    @pytest.mark.asyncio
    async def test_find_duplicates_empty(self):
        from services.contacts_hub.merge_engine import find_duplicates
        pool = FakePool(fetch_rows=[])
        result = await find_duplicates(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_find_duplicates_found(self):
        from services.contacts_hub.merge_engine import find_duplicates
        pool = FakePool(fetch_rows=[
            {"telegram_user_id": 123, "ids": ["id1", "id2"], "cnt": 2}
        ])
        result = await find_duplicates(pool, 123)
        assert len(result) == 1
        assert result[0]["count"] == 2
        assert result[0]["telegram_user_id"] == 123

    @pytest.mark.asyncio
    async def test_auto_merge_no_duplicates(self):
        from services.contacts_hub.merge_engine import auto_merge
        pool = FakePool(fetch_rows=[])
        result = await auto_merge(pool, 123)
        assert result["duplicates_found"] == 0
        assert result["contacts_merged"] == 0

    @pytest.mark.asyncio
    async def test_auto_merge_with_duplicates(self):
        from services.contacts_hub.merge_engine import auto_merge
        pool = FakePool(
            fetch_rows=[{"telegram_user_id": 123, "ids": ["id1", "id2"], "cnt": 2}],
            execute_val="UPDATE 1"
        )
        result = await auto_merge(pool, 123)
        assert result["duplicates_found"] == 1
        assert result["contacts_merged"] == 1

    @pytest.mark.asyncio
    async def test_manual_merge(self):
        from services.contacts_hub.merge_engine import manual_merge
        # Оба контакта "найдены" под owner_id=123 -> ownership-проверка проходит.
        pool = FakePool(fetch_row={"id": "primary-id"}, execute_val="UPDATE 1")
        result = await manual_merge(pool, "primary-id", "secondary-id", 123)
        assert result["status"] == "merged"
        assert result["primary_id"] == "primary-id"
        assert result["removed_id"] == "secondary-id"
        # 2x fetchrow (ownership) + 3x execute (2 UPDATE + 1 DELETE)
        assert len(pool._calls) == 5

    @pytest.mark.asyncio
    async def test_manual_merge_rejects_foreign_primary(self):
        # Регрессия: primary_id/secondary_id, не принадлежащие owner_id, раньше
        # молча переписывали contact_sources/contact_history чужого контакта
        # (межарендный IDOR) -- только финальный DELETE был скоупнут owner_id.
        from services.contacts_hub.merge_engine import manual_merge
        pool = FakePool(fetch_row=None, execute_val="UPDATE 1")  # владение не подтверждено
        result = await manual_merge(pool, "not-mine", "also-not-mine", 123)
        assert result["status"] == "error"
        assert result["error"] == "primary_not_found"
        # Ничего не должно было исполниться -- только проверка владения.
        assert all(call[0] == "fetchrow" for call in pool._calls)

    @pytest.mark.asyncio
    async def test_manual_merge_rejects_foreign_secondary(self):
        from services.contacts_hub.merge_engine import manual_merge

        class _OwnershipPool(FakePool):
            """primary принадлежит владельцу, secondary -- нет."""

            async def fetchrow(self, query, *args):
                self._calls.append(("fetchrow", query, args))
                contact_id = args[0]
                if contact_id == "primary-id":
                    return {"id": "primary-id"}
                return None

        pool = _OwnershipPool(execute_val="UPDATE 1")
        result = await manual_merge(pool, "primary-id", "foreign-id", 123)
        assert result["status"] == "error"
        assert result["error"] == "secondary_not_found"
        assert all(call[0] == "fetchrow" for call in pool._calls)


# ═══════════════════════════════════════════════════════════════════════════════
# Export Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExportEngine:
    """Tests for contacts_hub/export_engine.py"""

    @pytest.mark.asyncio
    async def test_export_csv_empty(self):
        from services.contacts_hub.export_engine import export_csv
        pool = FakePool(fetch_rows=[])
        result = await export_csv(pool, 123)
        assert "ID,Telegram User ID" in result

    @pytest.mark.asyncio
    async def test_export_csv_with_data(self):
        from services.contacts_hub.export_engine import export_csv
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "telegram_user_id": 123, "username": "ivan",
             "first_name": "Ivan", "last_name": "Ivanov", "display_name": "Ivan Ivanov",
             "phones": '["+123"]', "emails": "[]", "company": "TestCo",
             "position": "Dev", "websites": "[]", "birthday": None,
             "notes": "test note", "tags": "{vip}", "color_label": "#3b82f6",
             "is_favorite": True, "is_premium": False, "importance_level": None,
             "user_rating": None, "discovered_at": None, "last_synced_at": None,
             "created_at": None}
        ])
        result = await export_csv(pool, 123)
        assert "Ivan" in result
        assert "TestCo" in result
        assert "Dev" in result

    @pytest.mark.asyncio
    async def test_export_csv_with_ids_filter(self):
        from services.contacts_hub.export_engine import export_csv
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "telegram_user_id": 123, "username": None,
             "first_name": "Ivan", "last_name": None, "display_name": None,
             "phones": "[]", "emails": "[]", "company": None, "position": None,
             "websites": "[]", "birthday": None, "notes": None, "tags": "{}",
             "color_label": None, "is_favorite": False, "is_premium": False,
             "importance_level": None, "user_rating": None, "discovered_at": None,
             "last_synced_at": None, "created_at": None}
        ])
        result = await export_csv(pool, 123, contact_ids=["uuid-1"])
        assert "uuid-1" in result

    @pytest.mark.asyncio
    async def test_export_vcf_empty(self):
        from services.contacts_hub.export_engine import export_vcf
        pool = FakePool(fetch_rows=[])
        result = await export_vcf(pool, 123)
        assert result == ""

    @pytest.mark.asyncio
    async def test_export_vcf_with_data(self):
        from services.contacts_hub.export_engine import export_vcf
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "telegram_user_id": 123, "username": "ivan",
             "first_name": "Ivan", "last_name": "Ivanov", "company": "TestCo",
             "position": "Dev", "phones": '["+1234567"]', "emails": "[]",
             "websites": "[]", "notes": "Important client"}
        ])
        result = await export_vcf(pool, 123)
        assert "BEGIN:VCARD" in result
        assert "END:VCARD" in result
        assert "Ivan" in result
        assert "Ivanov" in result
        assert "+1234567" in result
        assert "TestCo" in result
        assert "Dev" in result
        assert "@ivan" in result
        assert "123" in result
        assert "Important client" in result

    @pytest.mark.asyncio
    async def test_export_vcf_multiple_phones(self):
        from services.contacts_hub.export_engine import export_vcf
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "telegram_user_id": 123, "username": None,
             "first_name": "Ivan", "last_name": None, "company": None,
             "position": None, "phones": '["+111", "+222"]', "emails": "[]",
             "websites": "[]", "notes": None}
        ])
        result = await export_vcf(pool, 123)
        assert "+111" in result
        assert "+222" in result

    @pytest.mark.asyncio
    async def test_export_json(self):
        from services.contacts_hub.export_engine import export_json
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "first_name": "Ivan", "phones": "[]", "emails": "[]",
             "websites": "[]", "addresses": "[]", "custom_fields": "{}",
             "digital_footprint": "{}", "created_at": None, "updated_at": None,
             "discovered_at": None, "last_synced_at": None, "last_changed_at": None}
        ])
        result = await export_json(pool, 123)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_export_json_with_ids_filter(self):
        from services.contacts_hub.export_engine import export_json
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "first_name": "Ivan", "phones": "[]", "emails": "[]",
             "websites": "[]", "addresses": "[]", "custom_fields": "{}",
             "digital_footprint": "{}", "created_at": None, "updated_at": None,
             "discovered_at": None, "last_synced_at": None, "last_changed_at": None}
        ])
        result = await export_json(pool, 123, contact_ids=["uuid-1"])
        data = json.loads(result)
        assert len(data) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Trust Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrustEngine:
    """Tests for contacts_hub/trust_engine.py"""

    def test_compute_trust_score_minimal(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        score = compute_trust_score({}, [])
        assert score == 0.5

    def test_compute_trust_score_basic(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"]}
        sources = [{"id": 1}, {"id": 2}]
        score = compute_trust_score(contact, sources)
        assert 0.5 <= score <= 1.0

    def test_compute_trust_score_full(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"]}
        sources = [{"id": 1}, {"id": 2}, {"id": 3}]
        score = compute_trust_score(contact, sources)
        assert score >= 0.9

    def test_compute_trust_score_no_phone(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {"telegram_user_id": 123, "username": "ivan", "phones": []}
        sources = [{"id": 1}, {"id": 2}]
        score = compute_trust_score(contact, sources)
        assert 0.5 < score < 1.0

    def test_compute_merge_confidence_same_telegram_id(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 123, "username": "ivan", "phones": [],
             "first_name": "Ivan", "last_name": "", "company": ""}
        b = {"telegram_user_id": 123, "username": "ivan2", "phones": [],
             "first_name": "Ivan", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] >= 0.5
        assert "same_telegram_id" in result["reasons"]

    def test_compute_merge_confidence_same_phone(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": ["+123"],
             "first_name": "A", "last_name": "", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": ["+123"],
             "first_name": "B", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_phone" in result["reasons"]
        assert result["confidence"] >= 0.3

    def test_compute_merge_confidence_same_username(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "ivan", "phones": [],
             "first_name": "A", "last_name": "", "company": ""}
        b = {"telegram_user_id": 2, "username": "ivan", "phones": [],
             "first_name": "B", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_username" in result["reasons"]

    def test_compute_merge_confidence_same_name(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [],
             "first_name": "Ivan", "last_name": "Ivanov", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": [],
             "first_name": "Ivan", "last_name": "Ivanov", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_name" in result["reasons"]

    def test_compute_merge_confidence_same_first_name_only(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [],
             "first_name": "Ivan", "last_name": "Petrov", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": [],
             "first_name": "Ivan", "last_name": "Sidorov", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_first_name" in result["reasons"]

    def test_compute_merge_confidence_same_company(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [],
             "first_name": "A", "last_name": "", "company": "Acme"}
        b = {"telegram_user_id": 2, "username": "b", "phones": [],
             "first_name": "B", "last_name": "", "company": "Acme"}
        result = compute_merge_confidence(a, b)
        assert "same_company" in result["reasons"]

    def test_compute_merge_confidence_low(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": ["+111"],
             "first_name": "A", "last_name": "X", "company": "C1"}
        b = {"telegram_user_id": 2, "username": "b", "phones": ["+222"],
             "first_name": "B", "last_name": "Y", "company": "C2"}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] < 0.5

    def test_compute_merge_confidence_multiple_reasons(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"],
             "first_name": "Ivan", "last_name": "Ivanov", "company": "Acme"}
        b = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"],
             "first_name": "Ivan", "last_name": "Ivanov", "company": "Acme"}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] >= 0.9
        assert len(result["reasons"]) >= 4

    @pytest.mark.asyncio
    async def test_detect_smart_duplicates_empty(self):
        from services.contacts_hub.trust_engine import detect_smart_duplicates
        pool = FakePool(fetch_rows=[])
        result = await detect_smart_duplicates(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_conflicts_empty(self):
        from services.contacts_hub.trust_engine import get_conflicts
        pool = FakePool(fetch_rows=[])
        result = await get_conflicts(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_conflicts_found(self):
        from services.contacts_hub.trust_engine import get_conflicts
        pool = FakePool(fetch_rows=[
            {"id": 1, "contact_id": "uuid-1", "resolution": "pending",
             "first_name": "Ivan", "last_name": "Ivanov", "username": "ivan"}
        ])
        result = await get_conflicts(pool, 123)
        assert len(result) == 1
        assert result[0]["resolution"] == "pending"

    @pytest.mark.asyncio
    async def test_resolve_conflict_success(self):
        from services.contacts_hub.trust_engine import resolve_conflict
        pool = FakePool(execute_val="UPDATE 1")
        result = await resolve_conflict(pool, 1, 123, "accepted", "Ivan")
        assert result is True

    @pytest.mark.asyncio
    async def test_resolve_conflict_not_found(self):
        from services.contacts_hub.trust_engine import resolve_conflict
        pool = FakePool(execute_val="UPDATE 0")
        result = await resolve_conflict(pool, 1, 123, "accepted")
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Versioning Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestVersioningEngine:
    """Tests for contacts_hub/versioning_engine.py"""

    @pytest.mark.asyncio
    async def test_get_versions_empty(self):
        from services.contacts_hub.versioning_engine import get_versions
        pool = FakePool(fetch_rows=[])
        result = await get_versions(pool, "uuid-1", 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_versions_with_data(self):
        from services.contacts_hub.versioning_engine import get_versions
        pool = FakePool(fetch_rows=[
            {"id": 1, "version_num": 2, "changed_fields": ["name"],
             "changed_by": "user", "change_summary": "Updated name",
             "created_at": None},
            {"id": 2, "version_num": 1, "changed_fields": [],
             "changed_by": "sync", "change_summary": None,
             "created_at": None},
        ])
        result = await get_versions(pool, "uuid-1", 123)
        assert len(result) == 2
        assert result[0]["version_num"] == 2

    @pytest.mark.asyncio
    async def test_get_version_detail_found(self):
        from services.contacts_hub.versioning_engine import get_version_detail
        pool = FakePool(fetch_row={
            "id": 1, "contact_id": "uuid-1", "version_num": 1,
            "snapshot": '{"first_name": "Ivan"}',
            "changed_fields": ["first_name"], "changed_by": "user",
            "change_summary": "Initial", "created_at": None,
        })
        result = await get_version_detail(pool, 1, 123)
        assert result is not None
        assert result["snapshot"]["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_get_version_detail_not_found(self):
        from services.contacts_hub.versioning_engine import get_version_detail
        pool = FakePool(fetch_row=None)
        result = await get_version_detail(pool, 999, 123)
        assert result is None

    @pytest.mark.asyncio
    async def test_rollback_to_version_not_found(self):
        from services.contacts_hub.versioning_engine import rollback_to_version
        pool = FakePool(fetch_row=None)
        result = await rollback_to_version(pool, "uuid-1", 123, 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_to_version_success(self):
        from services.contacts_hub.versioning_engine import rollback_to_version
        pool = FakePool(
            fetch_row={"snapshot": '{"first_name": "OldName", "phones": ["+123"]}'},
            execute_val="UPDATE 1",
            fetch_val=1,
        )
        result = await rollback_to_version(pool, "uuid-1", 123, 1)
        assert result is True
        assert len(pool._calls) >= 2

    @pytest.mark.asyncio
    async def test_get_contact_snapshot_found(self):
        from services.contacts_hub.versioning_engine import get_contact_snapshot
        pool = FakePool(fetch_row={
            "id": "uuid-1", "first_name": "Ivan", "phones": "[]", "emails": "[]",
        })
        result = await get_contact_snapshot(pool, "uuid-1")
        assert result["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_get_contact_snapshot_not_found(self):
        from services.contacts_hub.versioning_engine import get_contact_snapshot
        pool = FakePool(fetch_row=None)
        result = await get_contact_snapshot(pool, "nonexistent")
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════════
# CRM Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCRMEngine:
    """Tests for contacts_hub/crm_engine.py"""

    @pytest.mark.asyncio
    async def test_get_crm_data_none(self):
        from services.contacts_hub.crm_engine import get_crm_data
        pool = FakePool(fetch_row=None)
        result = await get_crm_data(pool, 123, "uuid-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_crm_data_exists(self):
        from services.contacts_hub.crm_engine import get_crm_data
        pool = FakePool(fetch_row={
            "id": 1, "stage": "lead", "deal_value": 100,
            "custom_fields": '{"key": "value"}', "currency": "USD",
        })
        result = await get_crm_data(pool, 123, "uuid-1")
        assert result["stage"] == "lead"
        assert result["custom_fields"]["key"] == "value"

    @pytest.mark.asyncio
    async def test_get_upcoming_reminders_empty(self):
        from services.contacts_hub.crm_engine import get_upcoming_reminders
        pool = FakePool(fetch_rows=[])
        result = await get_upcoming_reminders(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_upcoming_reminders_with_data(self):
        from services.contacts_hub.crm_engine import get_upcoming_reminders
        pool = FakePool(fetch_rows=[{
            "stage": "lead", "deal_value": 100, "first_name": "Ivan",
            "last_name": "Petrov", "username": "ivan",
            "next_reminder_at": None, "next_reminder_text": "Call",
        }])
        result = await get_upcoming_reminders(pool, 123)
        assert len(result) == 1
        assert result[0]["contact_name"] == "Ivan Petrov"

    @pytest.mark.asyncio
    async def test_get_crm_stats(self):
        from services.contacts_hub.crm_engine import get_crm_stats
        pool = FakePool(
            fetch_val=5,
            fetch_rows=[{"stage": "lead", "cnt": 3, "total_value": 100}]
        )
        result = await get_crm_stats(pool, 123)
        assert result["total_crm_contacts"] == 5
        assert len(result["by_stage"]) == 1

    @pytest.mark.asyncio
    async def test_get_crm_overdue_empty(self):
        from services.contacts_hub.crm_engine import get_crm_overdue
        pool = FakePool(fetch_rows=[])
        result = await get_crm_overdue(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_log_crm_activity(self):
        from services.contacts_hub.crm_engine import log_crm_activity
        pool = FakePool()
        await log_crm_activity(pool, 123, "uuid-1", "call", "Called client")
        assert len(pool._calls) == 1
        assert "INSERT INTO contact_crm_activity" in pool._calls[0][1]


# ═══════════════════════════════════════════════════════════════════════════════
# Smart Tags Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSmartTagsEngine:
    """Tests for contacts_hub/smart_tags_engine.py"""

    def test_check_condition_eq(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"is_premium": True}, {"field": "is_premium", "op": "eq", "value": True}) is True
        assert _check_condition({"is_premium": False}, {"field": "is_premium", "op": "eq", "value": True}) is False

    def test_check_condition_neq(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"x": 1}, {"field": "x", "op": "neq", "value": 2}) is True
        assert _check_condition({"x": 1}, {"field": "x", "op": "neq", "value": 1}) is False

    def test_check_condition_not_empty_list(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"phones": ["+123"]}, {"field": "phones", "op": "not_empty"}) is True
        assert _check_condition({"phones": []}, {"field": "phones", "op": "not_empty"}) is False

    def test_check_condition_not_empty_string(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"notes": "hello"}, {"field": "notes", "op": "not_empty"}) is True
        assert _check_condition({"notes": ""}, {"field": "notes", "op": "not_empty"}) is False
        assert _check_condition({"notes": "  "}, {"field": "notes", "op": "not_empty"}) is False

    def test_check_condition_not_empty_none(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"x": None}, {"field": "x", "op": "not_empty"}) is False

    def test_check_condition_gte(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"count": 5}, {"field": "count", "op": "gte", "value": 3}) is True
        assert _check_condition({"count": 2}, {"field": "count", "op": "gte", "value": 3}) is False
        assert _check_condition({"count": None}, {"field": "count", "op": "gte", "value": 3}) is False

    def test_check_condition_lte(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"count": 2}, {"field": "count", "op": "lte", "value": 5}) is True
        assert _check_condition({"count": 10}, {"field": "count", "op": "lte", "value": 5}) is False

    def test_check_condition_contains(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"notes": "important client"}, {"field": "notes", "op": "contains", "value": "client"}) is True
        assert _check_condition({"notes": "hello"}, {"field": "notes", "op": "contains", "value": "xyz"}) is False

    def test_check_condition_regex(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"phone": "+123456"}, {"field": "phone", "op": "regex", "value": r"^\+\d+"}) is True
        assert _check_condition({"phone": "abc"}, {"field": "phone", "op": "regex", "value": r"^\+\d+"}) is False

    def test_check_condition_regex_invalid(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"phone": "abc"}, {"field": "phone", "op": "regex", "value": "[invalid"}) is False

    def test_check_condition_unknown_op(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"x": 1}, {"field": "x", "op": "unknown"}) is False

    @pytest.mark.asyncio
    async def test_get_smart_tags_empty(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tags
        pool = FakePool(fetch_rows=[])
        result = await get_smart_tags(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_smart_tags_with_data(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tags
        pool = FakePool(fetch_rows=[
            {"tag": "vip", "cnt": 5, "source": "rule"},
            {"tag": "premium", "cnt": 3, "source": "builtin"},
        ])
        result = await get_smart_tags(pool, 123)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_smart_tag_rules_includes_builtin(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tag_rules
        pool = FakePool(fetch_rows=[])
        result = await get_smart_tag_rules(pool, 123)
        assert len(result) >= 7

    @pytest.mark.asyncio
    async def test_get_smart_tag_rules_with_custom(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tag_rules
        pool = FakePool(fetch_rows=[
            {"id": 1, "name": "VIP", "tag": "vip", "conditions": '{"field":"notes","op":"contains","value":"vip"}',
             "is_active": True}
        ])
        result = await get_smart_tag_rules(pool, 123)
        custom = [r for r in result if r.get("source") == "custom"]
        assert len(custom) == 1
        assert custom[0]["tag"] == "vip"

    @pytest.mark.asyncio
    async def test_create_smart_tag_rule(self):
        from services.contacts_hub.smart_tags_engine import create_smart_tag_rule
        pool = FakePool(fetch_val=42)
        rule_id = await create_smart_tag_rule(
            pool, 123, "VIP Rule", "vip",
            {"field": "is_premium", "op": "eq", "value": True}
        )
        assert rule_id == 42

    @pytest.mark.asyncio
    async def test_delete_smart_tag_rule_success(self):
        from services.contacts_hub.smart_tags_engine import delete_smart_tag_rule
        pool = FakePool(execute_val="DELETE 1")
        result = await delete_smart_tag_rule(pool, 1, 123)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_smart_tag_rule_not_found(self):
        from services.contacts_hub.smart_tags_engine import delete_smart_tag_rule
        pool = FakePool(execute_val="DELETE 0")
        result = await delete_smart_tag_rule(pool, 1, 123)
        assert result is False

    @pytest.mark.asyncio
    async def test_toggle_smart_tag_rule(self):
        from services.contacts_hub.smart_tags_engine import toggle_smart_tag_rule
        pool = FakePool(fetch_row={"is_active": False})
        result = await toggle_smart_tag_rule(pool, 1, 123)
        assert result is False

    @pytest.mark.asyncio
    async def test_toggle_smart_tag_rule_not_found(self):
        from services.contacts_hub.smart_tags_engine import toggle_smart_tag_rule
        pool = FakePool(fetch_row=None)
        result = await toggle_smart_tag_rule(pool, 1, 123)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Bulk Ops Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBulkOpsEngine:
    """Tests for contacts_hub/bulk_ops_engine.py"""

    @pytest.mark.asyncio
    async def test_bulk_tag(self):
        from services.contacts_hub.bulk_ops_engine import bulk_tag
        pool = FakePool(execute_val="UPDATE 1")
        result = await bulk_tag(pool, 123, ["id1", "id2"], "vip")
        assert result["updated"] == 2
        assert result["tag"] == "vip"

    @pytest.mark.asyncio
    async def test_bulk_tag_empty(self):
        from services.contacts_hub.bulk_ops_engine import bulk_tag
        pool = FakePool(execute_val="UPDATE 0")
        result = await bulk_tag(pool, 123, [], "vip")
        assert result["updated"] == 0

    @pytest.mark.asyncio
    async def test_bulk_untag(self):
        from services.contacts_hub.bulk_ops_engine import bulk_untag
        pool = FakePool(execute_val="UPDATE 1")
        result = await bulk_untag(pool, 123, ["id1"], "vip")
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_bulk_set_favorite(self):
        from services.contacts_hub.bulk_ops_engine import bulk_set_favorite
        pool = FakePool(execute_val="UPDATE 2")
        result = await bulk_set_favorite(pool, 123, ["id1", "id2"], True)
        assert result["updated"] == 2

    @pytest.mark.asyncio
    async def test_bulk_delete(self):
        from services.contacts_hub.bulk_ops_engine import bulk_delete
        pool = FakePool(execute_val="DELETE 3")
        result = await bulk_delete(pool, 123, ["id1", "id2", "id3"])
        assert result["deleted"] == 3

    @pytest.mark.asyncio
    async def test_bulk_delete_empty(self):
        from services.contacts_hub.bulk_ops_engine import bulk_delete
        pool = FakePool(execute_val="DELETE 0")
        result = await bulk_delete(pool, 123, [])
        assert result["deleted"] == 0

    @pytest.mark.asyncio
    async def test_bulk_add_to_group(self):
        from services.contacts_hub.bulk_ops_engine import bulk_add_to_group
        pool = FakePool(execute_val="INSERT 0 1")
        result = await bulk_add_to_group(pool, 123, ["id1", "id2"], 1)
        assert result["added"] == 2

    @pytest.mark.asyncio
    async def test_bulk_remove_from_group(self):
        from services.contacts_hub.bulk_ops_engine import bulk_remove_from_group
        pool = FakePool(execute_val="DELETE 1")
        result = await bulk_remove_from_group(pool, 123, ["id1"], 1)
        assert result["removed"] == 1

    @pytest.mark.asyncio
    async def test_create_group(self):
        from services.contacts_hub.bulk_ops_engine import create_group
        pool = FakePool(fetch_val=42)
        result = await create_group(pool, 123, "Clients", "#3b82f6")
        assert result == 42

    @pytest.mark.asyncio
    async def test_update_group_name(self):
        from services.contacts_hub.bulk_ops_engine import update_group
        pool = FakePool(execute_val="UPDATE 1")
        result = await update_group(pool, 1, 123, name="New Name")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_group_color(self):
        from services.contacts_hub.bulk_ops_engine import update_group
        pool = FakePool(execute_val="UPDATE 1")
        result = await update_group(pool, 1, 123, color="#ff0000")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_group_no_fields(self):
        from services.contacts_hub.bulk_ops_engine import update_group
        pool = FakePool()
        result = await update_group(pool, 1, 123)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_group_success(self):
        from services.contacts_hub.bulk_ops_engine import delete_group
        pool = FakePool(execute_val="DELETE 1")
        result = await delete_group(pool, 1, 123)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_group_not_found(self):
        from services.contacts_hub.bulk_ops_engine import delete_group
        pool = FakePool(execute_val="DELETE 0")
        result = await delete_group(pool, 1, 123)
        assert result is False

    @pytest.mark.asyncio
    async def test_bulk_merge_owned_pair_merges(self):
        # Раньше (до фикса IDOR) второй/третий execute шли БЕЗ проверки
        # владения -- сейчас перед ними обязана быть ownership-проверка.
        from services.contacts_hub.bulk_ops_engine import bulk_merge
        pool = FakePool(fetch_row={"cnt": 2}, execute_val="UPDATE 1")
        result = await bulk_merge(pool, 123, [{"primary_id": "a", "secondary_id": "b"}])
        assert result == {"merged": 1, "skipped": 0}
        kinds = [c[0] for c in pool._calls]
        assert kinds == ["fetchrow", "execute", "execute", "execute"]

    @pytest.mark.asyncio
    async def test_bulk_merge_foreign_pair_skipped_not_merged(self):
        # Регрессия межарендного IDOR: пара с чужим contact_id (владение не
        # подтверждено owner_id) обязана быть пропущена, а НЕ помечена merged,
        # и contact_sources/contact_history чужого контакта не трогаются.
        from services.contacts_hub.bulk_ops_engine import bulk_merge
        pool = FakePool(fetch_row={"cnt": 1}, execute_val="UPDATE 1")  # только 1 из 2 -- чужой
        result = await bulk_merge(pool, 123, [{"primary_id": "mine", "secondary_id": "not-mine"}])
        assert result == {"merged": 0, "skipped": 1}
        assert all(call[0] == "fetchrow" for call in pool._calls)

    @pytest.mark.asyncio
    async def test_bulk_merge_same_id_skipped(self):
        from services.contacts_hub.bulk_ops_engine import bulk_merge
        pool = FakePool(fetch_row={"cnt": 2}, execute_val="UPDATE 1")
        result = await bulk_merge(pool, 123, [{"primary_id": "a", "secondary_id": "a"}])
        assert result == {"merged": 0, "skipped": 1}
        assert pool._calls == []

    @pytest.mark.asyncio
    async def test_bulk_export_json_delegates(self):
        from services.contacts_hub.bulk_ops_engine import bulk_export
        pool = FakePool(fetch_rows=[])
        result = await bulk_export(pool, 123, ["id1"], "json")
        assert result["format"] == "json"

    @pytest.mark.asyncio
    async def test_bulk_set_importance(self):
        from services.contacts_hub.bulk_ops_engine import bulk_set_importance
        pool = FakePool(execute_val="UPDATE 2")
        result = await bulk_set_importance(pool, 123, ["id1", "id2"], 3)
        assert result["updated"] == 2
        # owner_id обязан участвовать в запросе -- иначе можно поднять importance
        # чужому контакту, зная только его id.
        _, query, params = pool._calls[-1]
        assert "owner_id=$2" in query or "owner_id=$2" in query.replace(" ", "")
        assert 123 in params

    @pytest.mark.asyncio
    async def test_bulk_set_rating(self):
        from services.contacts_hub.bulk_ops_engine import bulk_set_rating
        pool = FakePool(execute_val="UPDATE 1")
        result = await bulk_set_rating(pool, 123, ["id1"], 5)
        assert result["updated"] == 1
        _, query, params = pool._calls[-1]
        assert 123 in params


# ═══════════════════════════════════════════════════════════════════════════════
# Identity Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentityEngine:
    """Tests for contacts_hub/identity_engine.py"""

    @pytest.mark.asyncio
    async def test_build_identity_graph_not_found(self):
        from services.contacts_hub.identity_engine import build_identity_graph
        pool = FakePool(fetch_row=None)
        result = await build_identity_graph(pool, 123, "uuid-1")
        assert result == {}

    @pytest.mark.asyncio
    async def test_build_identity_graph_full_contact(self):
        from services.contacts_hub.identity_engine import build_identity_graph
        pool = FakePool(
            fetch_row={
                "id": "uuid-1", "telegram_user_id": 123, "username": "ivan",
                "first_name": "Ivan", "last_name": "Ivanov",
                "phones": '["+123"]', "emails": '["a@b.com"]',
                "websites": '["https://ivan.com"]',
            },
            fetch_rows=[{"account_id": 1, "first_name": "Ivan", "phone": "+123",
                         "local_name": "Ivan"}]
        )
        result = await build_identity_graph(pool, 123, "uuid-1")
        assert result["contact_id"] == "uuid-1"
        assert result["total_count"] >= 5
        assert result["primary_count"] >= 2

    @pytest.mark.asyncio
    async def test_build_identity_graph_minimal(self):
        from services.contacts_hub.identity_engine import build_identity_graph
        pool = FakePool(
            fetch_row={"id": "uuid-1", "telegram_user_id": None, "username": None,
                       "phones": "[]", "emails": "[]", "websites": "[]"},
            fetch_rows=[]
        )
        result = await build_identity_graph(pool, 123, "uuid-1")
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_get_identity_graph_empty(self):
        from services.contacts_hub.identity_engine import get_identity_graph
        pool = FakePool(fetch_rows=[])
        result = await get_identity_graph(pool, 123, "uuid-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_identity_graph_with_data(self):
        from services.contacts_hub.identity_engine import get_identity_graph
        pool = FakePool(fetch_rows=[
            {"identity_type": "telegram_id", "identity_value": "123", "is_primary": True},
            {"identity_type": "username", "identity_value": "@ivan", "is_primary": True},
        ])
        result = await get_identity_graph(pool, 123, "uuid-1")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_last_active_none(self):
        from services.contacts_hub.identity_engine import get_last_active
        pool = FakePool(fetch_row={"last_active_at": None})
        result = await get_last_active(pool, "uuid-1")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Relationship Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRelationshipEngine:
    """Tests for contacts_hub/relationship_engine.py"""

    @pytest.mark.asyncio
    async def test_compute_relationships_empty(self):
        from services.contacts_hub.relationship_engine import compute_relationships
        pool = FakePool(fetch_rows=[])
        result = await compute_relationships(pool, 123)
        assert result["count"] == 0
        assert result["relationships"] == []

    @pytest.mark.asyncio
    async def test_compute_relationships_single_contact(self):
        from services.contacts_hub.relationship_engine import compute_relationships
        pool = FakePool(fetch_rows=[
            {"id": "uuid-1", "telegram_user_id": 123, "username": "ivan",
             "phones": ["+123"], "company": "Acme"}
        ])
        result = await compute_relationships(pool, 123)
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_relationships_empty(self):
        from services.contacts_hub.relationship_engine import get_relationships
        pool = FakePool(fetch_rows=[])
        result = await get_relationships(pool, 123, "uuid-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_graph_stats_zero(self):
        from services.contacts_hub.relationship_engine import get_graph_stats
        pool = FakePool(fetch_val=0, fetch_rows=[])
        result = await get_graph_stats(pool, 123)
        assert result["total_relationships"] == 0
        assert result["by_type"] == []
        assert result["strongest"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# AI Assistant Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAIAssistant:
    """Tests for contacts_hub/ai_assistant.py"""

    def test_match_query_count(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("сколько контактов")
        assert result is not None
        assert result["handler"] == "count_all"

    def test_match_query_premium(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("сколько контактов с premium")
        assert result is not None
        assert result["handler"] in ("count_premium", "count_all")

    def test_match_query_duplicates(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("покажи дубликаты")
        assert result is not None
        assert result["handler"] == "find_duplicates"

    def test_match_query_favorites(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("избранные")
        assert result is not None
        assert result["handler"] == "favorites"

    def test_match_query_reminders(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("напоминания")
        assert result is not None
        assert result["handler"] == "reminders"

    def test_match_query_stats(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("статистика")
        assert result is not None
        assert result["handler"] == "stats"

    def test_match_query_unknown(self):
        from services.contacts_hub.ai_assistant import _match_query
        result = _match_query("привет мир")
        assert result is None

    @pytest.mark.asyncio
    async def test_process_ai_query_count(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_val=42)
        result = await process_ai_query(pool, 123, "сколько контактов")
        assert "42" in result["answer"]
        assert result["type"] == "count"

    @pytest.mark.asyncio
    async def test_process_ai_query_premium_count(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_val=10)
        result = await process_ai_query(pool, 123, "сколько контактов с premium")
        assert "10" in result["answer"]

    @pytest.mark.asyncio
    async def test_process_ai_query_empty(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool()
        result = await process_ai_query(pool, 123, "абракадабра")
        assert result["type"] == "help"

    @pytest.mark.asyncio
    async def test_process_ai_query_favorites(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Ivan", "last_name": "", "username": "ivan"},
        ])
        result = await process_ai_query(pool, 123, "избранные")
        assert result["type"] == "list"
        assert len(result["contacts"]) == 1

    @pytest.mark.asyncio
    async def test_process_ai_query_by_tag(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Ivan", "last_name": "", "username": "ivan"},
        ])
        result = await process_ai_query(pool, 123, "тег vip")
        assert result["type"] == "list"
        assert "vip" in result["answer"]

    @pytest.mark.asyncio
    async def test_process_ai_query_by_company(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Ivan", "last_name": "", "username": "ivan"},
        ])
        result = await process_ai_query(pool, 123, "компания Acme")
        assert result["type"] == "list"

    @pytest.mark.asyncio
    async def test_process_ai_query_sync_status(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool(fetch_row={"last_sync": None, "total": 42})
        result = await process_ai_query(pool, 123, "последняя синхронизация")
        assert result["type"] == "status"


# ═══════════════════════════════════════════════════════════════════════════════
# Stats Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatsEngine:
    """Tests for contacts_hub/stats_engine.py"""

    @pytest.mark.asyncio
    async def test_get_full_stats(self):
        from services.contacts_hub.stats_engine import get_full_stats
        pool = FakePool(fetch_val=10, fetch_rows=[{"account_id": 1, "cnt": 5}])
        result = await get_full_stats(pool, 123)
        assert "total" in result

    @pytest.mark.asyncio
    async def test_get_account_stats_empty(self):
        from services.contacts_hub.stats_engine import get_account_stats
        pool = FakePool(fetch_rows=[])
        result = await get_account_stats(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_account_stats_with_data(self):
        from services.contacts_hub.stats_engine import get_account_stats
        pool = FakePool(fetch_rows=[
            {"account_id": 1, "contact_count": 50, "account_name": "Ivan", "account_phone": "+123"},
            {"account_id": 2, "contact_count": 20, "account_name": "Petr", "account_phone": "+456"},
        ])
        result = await get_account_stats(pool, 123)
        assert len(result) == 2
        assert result[0]["contact_count"] == 50


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Service Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncService:
    """Tests for contacts_hub/sync_service.py"""

    @pytest.mark.asyncio
    async def test_sync_account_no_session(self):
        from services.contacts_hub.sync_service import sync_account
        pool = FakePool(fetch_row=None)
        result = await sync_account(pool, 123, 5)
        assert "error" in result
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_all_accounts_no_accounts(self):
        from services.contacts_hub.sync_service import sync_all_accounts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        result = await sync_all_accounts(pool, 123)
        assert result["accounts_found"] == 0
        assert result["total_synced"] == 0
        assert "message" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Mini App API Pure Function Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMiniAppAPIPureFunctions:
    """Tests for pure helper functions in mini_app_api.py"""

    def test_is_safe_public_url_valid(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://example.com/photo.jpg") is True

    def test_is_safe_public_url_http(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("http://example.com/photo.jpg") is False

    def test_is_safe_public_url_localhost(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://localhost/photo.jpg") is False

    def test_is_safe_public_url_private_ip(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://192.168.1.1/photo.jpg") is False

    def test_is_safe_public_url_10_x(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://10.0.0.1/photo.jpg") is False

    def test_is_safe_public_url_172_16(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://172.16.0.1/photo.jpg") is False

    def test_is_safe_public_url_127(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://127.0.0.1/photo.jpg") is False

    def test_is_safe_public_url_empty(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("") is False
        assert is_safe_public_url(None) is False

    def test_is_safe_public_url_dot_local(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://myhost.local/photo.jpg") is False

    def test_schedule_repeat_minutes(self):
        from services.mini_app_api import schedule_repeat_minutes
        assert schedule_repeat_minutes("none") == 0
        assert schedule_repeat_minutes("daily") == 1440
        assert schedule_repeat_minutes("weekly") == 10080
        assert schedule_repeat_minutes(None) == 0
        assert schedule_repeat_minutes("unknown") == 0

    def test_parse_proxy_type_socks5(self):
        from services.mini_app_api import parse_proxy_type
        assert parse_proxy_type("socks5://host:1080") == "socks5"

    def test_parse_proxy_type_socks4(self):
        from services.mini_app_api import parse_proxy_type
        assert parse_proxy_type("socks4://host:1080") == "socks4"

    def test_parse_proxy_type_http(self):
        from services.mini_app_api import parse_proxy_type
        assert parse_proxy_type("http://host:8080") == "http"

    def test_parse_proxy_type_unknown(self):
        from services.mini_app_api import parse_proxy_type
        assert parse_proxy_type("ftp://host") is None
        assert parse_proxy_type("") is None
        assert parse_proxy_type(None) is None

    def test_channel_edit_worker_op(self):
        from services.mini_app_api import channel_edit_worker_op
        assert channel_edit_worker_op("title") == "chan_title"
        assert channel_edit_worker_op("about") == "chan_about"
        assert channel_edit_worker_op("username") == "chan_uname"
        assert channel_edit_worker_op("unknown") is None

    def test_parsed_audience_filters_empty(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({})
        assert sql == ""
        assert params == []

    def test_parsed_audience_filters_source(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"source": "admin"})
        assert "source_username" in sql
        assert "%admin%" in params

    def test_parsed_audience_filters_premium(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"premium": "1"})
        assert "is_premium=TRUE" in sql

    def test_parsed_audience_filters_with_username(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"with_username": "true"})
        assert "username IS NOT NULL" in sql

    def test_parsed_audience_filters_not_bot(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"not_bot": "1"})
        assert "is_bot" in sql

    def test_parsed_audience_filters_active(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"active": "true"})
        assert "is_active=TRUE" in sql

    def test_parsed_audience_filters_with_phone(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"with_phone": "1"})
        assert "phone IS NOT NULL" in sql

    def test_parsed_audience_filters_base_offset(self):
        from services.mini_app_api import parsed_audience_filters
        sql, params = parsed_audience_filters({"source": "admin"}, base_params_count=3)
        assert "$4" in sql

    def test_jlist_list(self):
        from services.mini_app_api import _jlist
        assert _jlist(["a", "b"]) == ["a", "b"]

    def test_jlist_none(self):
        from services.mini_app_api import _jlist
        assert _jlist(None) == []

    def test_jlist_json_string(self):
        from services.mini_app_api import _jlist
        assert _jlist('["a", "b"]') == ["a", "b"]

    def test_jlist_invalid_json(self):
        from services.mini_app_api import _jlist
        assert _jlist("not json") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Ranking Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRankingEngine:
    """Tests for ranking_engine.py"""

    @pytest.mark.asyncio
    async def test_ranking_track(self):
        from services.ranking_engine import track_keyword
        pool = FakePool(fetch_row={"id": 42})
        result = await track_keyword(pool, 123, "telegram bot", channel_id=100)
        assert result["ok"] is True
        assert result["id"] == 42
        assert "INSERT" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_ranking_record(self):
        from services.ranking_engine import record_position
        pool = FakePool(fetch_val=5, execute_val="INSERT 0 1")
        result = await record_position(pool, 123, 100, "telegram bot", 3)
        assert result["ok"] is True
        assert result["current"] == 3
        assert result["previous"] == 5

    @pytest.mark.asyncio
    async def test_ranking_history(self):
        from services.ranking_engine import get_position_history
        pool = FakePool(fetch_rows=[
            {"position": 5, "previous_position": None, "checked_at": None, "metadata": "{}"},
            {"position": 3, "previous_position": 5, "checked_at": None, "metadata": "{}"},
        ])
        result = await get_position_history(pool, 123, 100, "telegram bot")
        assert len(result) == 2
        assert result[1]["position"] == 3

    @pytest.mark.asyncio
    async def test_ranking_stats(self):
        from services.ranking_engine import get_ranking_stats
        pool = FakePool(fetch_val=10)
        result = await get_ranking_stats(pool, 123)
        assert "total_tracked" in result
        assert "total_checks" in result
        assert "avg_position_7d" in result
        assert "alerts_pending" in result

    @pytest.mark.asyncio
    async def test_ranking_track_error(self):
        from services.ranking_engine import track_keyword
        pool = FakePool(error=Exception("DB error"))
        result = await track_keyword(pool, 123, "test")
        assert result["ok"] is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Network Builder Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNetworkBuilder:
    """Tests for network_builder.py"""

    @pytest.mark.asyncio
    async def test_network_template_create(self):
        from services.network_builder import create_template
        pool = FakePool(fetch_row={"id": 1})
        result = await create_template(
            pool, 123, "My Network", "Test network",
            template_type="channel_group",
            nodes=[{"label": "Channel 1", "type": "channel"}],
            edges=[],
        )
        assert result["ok"] is True
        assert result["id"] == 1
        assert "INSERT" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_network_templates(self):
        from services.network_builder import get_templates
        pool = FakePool(fetch_rows=[
            {"id": 1, "name": "Network A", "template_type": "channel_group"},
            {"id": 2, "name": "Network B", "template_type": "bot_network"},
        ])
        result = await get_templates(pool, 123)
        assert len(result) == 2
        assert result[0]["name"] == "Network A"

    @pytest.mark.asyncio
    async def test_network_instance_create(self):
        from services.network_builder import create_instance
        pool = FakePool(
            fetch_row={"id": 1, "nodes": "[]", "edges": "[]"},
            fetch_rows=[{"id": 10, "nodes": "[]", "edges": "[]", "name": "Inst"}],
        )
        result = await create_instance(pool, 123, 1, "Instance 1")
        assert result["ok"] is True
        assert "id" in result

    @pytest.mark.asyncio
    async def test_network_instance_create_not_found(self):
        from services.network_builder import create_instance
        pool = FakePool(fetch_row=None)
        result = await create_instance(pool, 123, 999, "Instance 1")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_network_stats(self):
        from services.network_builder import get_network_stats
        pool = FakePool(fetch_val=5)
        result = await get_network_stats(pool, 123)
        assert "templates" in result
        assert "instances" in result
        assert "total_nodes" in result
        assert "total_edges" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Workflow Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowEngine:
    """Tests for workflow_engine.py"""

    @pytest.mark.asyncio
    async def test_workflow_create(self):
        from services.workflow_engine import create_workflow
        pool = FakePool(fetch_row={"id": 1})
        result = await create_workflow(
            pool, 123, "Publish Workflow",
            steps=[{"action": "post", "target": "channel"}, {"action": "wait"}],
        )
        assert result["ok"] is True
        assert result["id"] == 1
        assert "INSERT" in pool._calls[0][1]

    @pytest.mark.asyncio
    async def test_workflow_execute(self):
        from services.workflow_engine import execute_workflow
        pool = FakePool(
            fetch_row={"id": 1, "steps": '[{"action": "post"}]'},
            fetch_rows=[{"id": 1, "steps": '[{"action": "post"}]'}],
        )
        result = await execute_workflow(pool, 123, 1, {"channel": "test"})
        assert result["ok"] is True
        assert "run_id" in result
        assert "total_steps" in result

    @pytest.mark.asyncio
    async def test_workflow_execute_not_found(self):
        from services.workflow_engine import execute_workflow
        pool = FakePool(fetch_row=None)
        result = await execute_workflow(pool, 123, 999)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_workflow_status(self):
        from services.workflow_engine import get_workflow_status
        pool = FakePool(fetch_row={
            "id": 1, "status": "running", "current_step": 0,
            "total_steps": 2, "workflow_name": "Publish",
        })
        result = await get_workflow_status(pool, 123, 1)
        assert result is not None
        assert result["status"] == "running"
        assert result["workflow_name"] == "Publish"

    @pytest.mark.asyncio
    async def test_workflow_status_not_found(self):
        from services.workflow_engine import get_workflow_status
        pool = FakePool(fetch_row=None)
        result = await get_workflow_status(pool, 123, 999)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Audience Analytics Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAudienceAnalytics:
    """Tests for audience_analytics.py"""

    @pytest.mark.asyncio
    async def test_audience_analyze(self):
        from services.audience_analytics import analyze_audience
        pool = FakePool(fetch_val=1000)
        result = await analyze_audience(pool, 123, 1001)
        assert result is not None
        assert result.channel_id == 1001
        assert result.owner_id == 123
        assert result.total_subscribers >= 0

    @pytest.mark.asyncio
    async def test_audience_segment(self):
        from services.audience_analytics import segment_audience
        pool = FakePool(fetch_rows=[])
        result = await segment_audience(pool, 123, 1001)
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Analytics Dashboard Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsDashboard:
    """Tests for analytics_dashboard.py"""

    @pytest.mark.asyncio
    async def test_analytics_dashboard(self):
        from services.analytics_dashboard import get_dashboard_stats
        pool = FakePool(fetch_rows=[
            {"total": 10, "active": 8, "banned": 1, "spamblock": 1,
             "success": 9, "failed": 1, "running": 2,
             "total_users": 100, "new_24h": 5, "new_7d": 30,
             "avg_trust": 0.85, "at_risk": 1, "total_usd": 50.0},
        ])
        result = await get_dashboard_stats(pool, 123)
        assert "accounts" in result
        assert "operations_24h" in result
        assert "audience" in result

    @pytest.mark.asyncio
    async def test_analytics_realtime(self):
        from services.analytics_dashboard import get_realtime_metrics
        pool = FakePool(fetch_rows=[
            {"id": 1, "op_type": "test", "status": "running", "started_at": None, "params": "{}",
             "action": "post", "result": "success", "target": "channel",
             "occurred_at": None, "cnt": 5},
        ])
        result = await get_realtime_metrics(pool, 123)
        assert "active_operations" in result
        assert "recent_events" in result
        assert "account_status" in result
        assert "queue_depth" in result

    @pytest.mark.asyncio
    async def test_analytics_historical(self):
        from services.analytics_dashboard import get_historical_data
        pool = FakePool(fetch_rows=[
            {"day": None, "total": 10, "success": 8, "failed": 2,
             "new_users": 5, "avg_health": 0.8, "avg_trust": 0.75},
        ])
        result = await get_historical_data(pool, 123, "operations", days=7)
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Mass Messaging Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMassMessaging:
    """Tests for broadcaster.py mass messaging functions."""

    @pytest.mark.asyncio
    async def test_broadcast_schedule_immediate(self):
        from services.broadcaster import mass_broadcast_with_scheduling
        pool = FakePool(
            fetch_row={"id": 42, "bot_id": 1, "token": "tok", "username": "testbot"},
            fetch_val=42,
            fetch_rows=[],
        )
        with patch("services.broadcaster.db") as mock_db:
            mock_db.safe_count = AsyncMock(return_value=100)
            result = await mass_broadcast_with_scheduling(
                pool, 123, 1, "Hello!", {"segment": "all"}
            )
        assert result["ok"] is True
        assert result["total_users"] == 100
        assert result["scheduled_minutes"] == 0

    @pytest.mark.asyncio
    async def test_broadcast_schedule_delayed(self):
        from services.broadcaster import mass_broadcast_with_scheduling
        pool = FakePool(
            fetch_row={"id": 42, "bot_id": 1, "token": "tok", "username": "testbot"},
            fetch_val=42,
        )
        with patch("services.broadcaster.db") as mock_db:
            mock_db.safe_count = AsyncMock(return_value=50)
            result = await mass_broadcast_with_scheduling(
                pool, 123, 1, "Hello!", {"schedule_minutes": 30}
            )
        assert result["ok"] is True
        assert result["scheduled_minutes"] == 30

    @pytest.mark.asyncio
    async def test_broadcast_schedule_bot_not_found(self):
        from services.broadcaster import mass_broadcast_with_scheduling
        pool = FakePool(fetch_row=None)
        result = await mass_broadcast_with_scheduling(
            pool, 123, 999, "Hello!", {}
        )
        assert result["ok"] is False
        assert "Bot not found" in result["error"]

    @pytest.mark.asyncio
    async def test_broadcast_schedule_suspicious_text(self):
        from services.broadcaster import mass_broadcast_with_scheduling
        pool = FakePool()
        with patch("services.broadcaster.check_sql_suspicious", return_value=True):
            result = await mass_broadcast_with_scheduling(
                pool, 123, 1, "'; DROP TABLE--", {}
            )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_ab_test_no_variants(self):
        from services.broadcaster import ab_test_broadcast
        pool = FakePool()
        result = await ab_test_broadcast(pool, 123, 1, [])
        assert result["ok"] is False
        assert "variants" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_ab_test_too_many_variants(self):
        from services.broadcaster import ab_test_broadcast
        pool = FakePool()
        variants = [{"text": f"msg{i}", "weight": 1} for i in range(15)]
        result = await ab_test_broadcast(pool, 123, 1, variants)
        assert result["ok"] is False
        assert "Max 10" in result["error"]

    @pytest.mark.asyncio
    async def test_ab_test_bot_not_found(self):
        from services.broadcaster import ab_test_broadcast
        pool = FakePool(fetch_row=None)
        result = await ab_test_broadcast(
            pool, 123, 999, [{"text": "A", "weight": 1}]
        )
        assert result["ok"] is False
        assert "Bot not found" in result["error"]

    @pytest.mark.asyncio
    async def test_ab_test_no_subscribers(self):
        from services.broadcaster import ab_test_broadcast
        pool = FakePool(
            fetch_row={"bot_id": 1, "token": "tok", "username": "testbot"},
        )
        with patch("services.broadcaster.db") as mock_db:
            mock_db.get_audience_user_ids = AsyncMock(return_value=[])
            result = await ab_test_broadcast(
                pool, 123, 1, [{"text": "A", "weight": 1}]
            )
        assert result["ok"] is False
        assert "No active subscribers" in result["error"]

    @pytest.mark.asyncio
    async def test_ab_test_success(self):
        from services.broadcaster import ab_test_broadcast
        pool = FakePool(
            fetch_row={"id": 10, "bot_id": 1, "token": "tok", "username": "testbot"},
            fetch_val=20,
        )
        with patch("services.broadcaster.db") as mock_db:
            mock_db.get_audience_user_ids = AsyncMock(
                return_value=list(range(1, 101))
            )
            with patch("services.broadcaster.check_sql_suspicious", return_value=False):
                result = await ab_test_broadcast(
                    pool, 123, 1, [
                        {"text": "Variant A", "weight": 1},
                        {"text": "Variant B", "weight": 1},
                    ]
                )
        assert result["ok"] is True
        assert result["total_users"] == 100
        assert len(result["broadcasts"]) >= 1

    @pytest.mark.asyncio
    async def test_broadcast_analytics_found(self):
        from services.broadcaster import get_broadcast_analytics
        pool = FakePool(
            fetch_row={
                "id": 1, "bot_id": 1, "message_text": "Hello",
                "status": "done", "sent_count": 95, "failed_count": 5,
                "total_users": 100, "created_at": None,
                "buttons": None, "silent": False, "bot_username": "testbot",
            },
            fetch_rows=[
                {"user_id": 1, "sent_at": None},
                {"user_id": 2, "sent_at": None},
            ],
        )
        result = await get_broadcast_analytics(pool, 123, 1)
        assert result["ok"] is True
        assert result["sent_count"] == 95
        assert result["failed_count"] == 5
        assert result["total_users"] == 100
        assert result["delivery_rate_pct"] == 95.0
        assert result["bot_username"] == "testbot"

    @pytest.mark.asyncio
    async def test_broadcast_analytics_not_found(self):
        from services.broadcaster import get_broadcast_analytics
        pool = FakePool(fetch_row=None)
        result = await get_broadcast_analytics(pool, 123, 999)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_broadcast_analytics_zero_total(self):
        from services.broadcaster import get_broadcast_analytics
        pool = FakePool(
            fetch_row={
                "id": 1, "bot_id": 1, "message_text": "Hello",
                "status": "done", "sent_count": 0, "failed_count": 0,
                "total_users": 0, "created_at": None,
                "buttons": None, "silent": False, "bot_username": "testbot",
            },
            fetch_rows=[],
        )
        result = await get_broadcast_analytics(pool, 123, 1)
        assert result["ok"] is True
        assert result["delivery_rate_pct"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Proxy Pool Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestProxyPool:
    """Tests for proxy_selector.py and proxy_hygiene.py proxy pool functions."""

    @pytest.mark.asyncio
    async def test_proxy_pool_stats_empty(self):
        from services.proxy_selector import get_proxy_pool_stats
        pool = FakePool(fetch_rows=[])
        result = await get_proxy_pool_stats(pool, 123)
        assert result["total"] == 0
        assert result["active"] == 0
        assert result["dead"] == 0

    @pytest.mark.asyncio
    async def test_proxy_pool_stats_with_proxies(self):
        from services.proxy_selector import get_proxy_pool_stats
        pool = FakePool(fetch_rows=[
            {
                "id": 1, "is_active": True, "is_alive": True,
                "geo_country": "US", "proxy_url": "enc1", "assigned_count": 2,
            },
            {
                "id": 2, "is_active": True, "is_alive": False,
                "geo_country": "DE", "proxy_url": "enc2", "assigned_count": 0,
            },
            {
                "id": 3, "is_active": False, "is_alive": True,
                "geo_country": "US", "proxy_url": "enc3", "assigned_count": 1,
            },
        ])
        with patch("services.proxy_selector.get_proxy_score", return_value=0.7):
            result = await get_proxy_pool_stats(pool, 123)
        assert result["total"] == 3
        assert result["active"] == 2
        assert result["inactive"] == 1
        assert result["dead"] == 1
        assert result["assigned"] == 2
        assert result["unassigned"] == 1
        assert result["avg_score"] == 0.7
        assert result["geo_distribution"]["US"] == 2
        assert result["geo_distribution"]["DE"] == 1

    @pytest.mark.asyncio
    async def test_proxy_pool_stats_db_error(self):
        from services.proxy_selector import get_proxy_pool_stats
        pool = FakePool(error=Exception("DB down"))
        # Уникальный owner_id: кэш pps:{owner_id} у success-теста (owner 123)
        # иначе вернул бы закэшированный total=3, минуя ветку ошибки.
        result = await get_proxy_pool_stats(pool, 998877)
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_auto_rotate_no_account(self):
        from services.proxy_selector import auto_rotate_proxy
        pool = FakePool(fetch_row=None)
        result = await auto_rotate_proxy(pool, 123, 999)
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_auto_rotate_no_proxies(self):
        from services.proxy_selector import auto_rotate_proxy
        pool = FakePool(
            fetch_row={"id": 1, "proxy_id": 10},
            fetch_rows=[],
        )
        result = await auto_rotate_proxy(pool, 123, 1)
        assert result["success"] is False
        assert "no active proxies" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_auto_rotate_success(self):
        from services.proxy_selector import auto_rotate_proxy
        pool = FakePool(
            fetch_row={"id": 1, "proxy_id": 10},
            fetch_rows=[
                {"id": 10, "proxy_url": "enc1", "is_alive": True, "is_active": True},
            ],
        )
        with patch("services.proxy_selector.get_proxy_score", return_value=0.8), \
             patch("services.proxy_selector.extract_ip_from_proxy", return_value="1.2.3.4"):
            result = await auto_rotate_proxy(pool, 123, 1)
        assert result["success"] is True
        assert result["new_proxy_id"] == 10
        assert result["old_proxy_id"] == 10

    @pytest.mark.asyncio
    async def test_cleanup_dead_no_proxies(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(fetch_rows=[])
        result = await cleanup_dead_proxies(pool, 123)
        assert result["removed_count"] == 0
        assert result["removed_ids"] == []

    @pytest.mark.asyncio
    async def test_cleanup_dead_removes_unassigned_dead(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(
            fetch_rows=[
                {"id": 1, "is_active": True, "is_alive": False, "assigned_count": 0},
                {"id": 2, "is_active": True, "is_alive": False, "assigned_count": 0},
            ],
            execute_val="DELETE 1",
        )
        result = await cleanup_dead_proxies(pool, 123)
        assert result["removed_count"] == 2
        assert 1 in result["removed_ids"]
        assert 2 in result["removed_ids"]

    @pytest.mark.asyncio
    async def test_cleanup_dead_skips_assigned(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(
            fetch_rows=[
                {"id": 1, "is_active": True, "is_alive": False, "assigned_count": 3},
                {"id": 2, "is_active": True, "is_alive": False, "assigned_count": 0},
            ],
            execute_val="DELETE 1",
        )
        result = await cleanup_dead_proxies(pool, 123)
        assert result["removed_count"] == 1
        assert result["skipped_assigned"] == 1
        assert 2 in result["removed_ids"]

    @pytest.mark.asyncio
    async def test_cleanup_dead_skips_alive(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(
            fetch_rows=[
                {"id": 1, "is_active": True, "is_alive": True, "assigned_count": 0},
            ],
        )
        result = await cleanup_dead_proxies(pool, 123)
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_dead_skips_null_alive(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(
            fetch_rows=[
                {"id": 1, "is_active": True, "is_alive": None, "assigned_count": 0},
            ],
        )
        result = await cleanup_dead_proxies(pool, 123)
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_dead_db_error(self):
        from services.proxy_hygiene import cleanup_dead_proxies
        pool = FakePool(error=Exception("DB error"))
        result = await cleanup_dead_proxies(pool, 123)
        assert result["errors"]
        assert result["removed_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-Registration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoRegistration:
    """Tests for auto_registrar.py batch registration and stats."""

    @pytest.mark.asyncio
    async def test_batch_register_no_api_key(self):
        from bot.handlers.auto_registrar import batch_register_with_scheduling
        pool = FakePool()
        with patch("bot.handlers.auto_registrar.db") as mock_db:
            mock_db.get_platform_setting = AsyncMock(return_value="")
            result = await batch_register_with_scheduling(pool, 123, 5)
        assert result["status"] == "no_api_key"
        assert result["count"] == 0
        assert result["task_id"] is None

    @pytest.mark.asyncio
    async def test_batch_register_scheduled(self):
        from bot.handlers.auto_registrar import batch_register_with_scheduling
        pool = FakePool(fetch_val=100)
        with patch("bot.handlers.auto_registrar.db") as mock_db:
            mock_db.get_platform_setting = AsyncMock(return_value="real_key")
            mock_db.create_scheduled = AsyncMock(return_value=100)
            result = await batch_register_with_scheduling(
                pool, 123, 10,
                {"execute_at": "2025-01-01T12:00:00", "country": "russia"},
            )
        assert result["task_id"] == 100
        assert result["status"] == "queued"
        assert result["count"] == 10

    @pytest.mark.asyncio
    async def test_batch_register_with_repetition(self):
        from bot.handlers.auto_registrar import batch_register_with_scheduling
        pool = FakePool(fetch_val=200)
        with patch("bot.handlers.auto_registrar.db") as mock_db:
            mock_db.get_platform_setting = AsyncMock(return_value="real_key")
            mock_db.create_scheduled = AsyncMock(return_value=200)
            result = await batch_register_with_scheduling(
                pool, 123, 5,
                {"execute_at": "2025-01-01T12:00:00", "interval_minutes": 60},
            )
        assert result["task_id"] == 200
        assert result["status"] == "queued"
        assert len(pool._calls) >= 1

    @pytest.mark.asyncio
    async def test_registration_stats_empty(self):
        from bot.handlers.auto_registrar import get_registration_stats
        pool = FakePool(fetch_row=None)
        result = await get_registration_stats(pool, 123)
        assert result == {}

    @pytest.mark.asyncio
    async def test_registration_stats_with_data(self):
        from bot.handlers.auto_registrar import get_registration_stats
        pool = FakePool(fetch_row={
            "total": 50,
            "active": 40,
            "inactive": 8,
            "banned": 2,
            "added_today": 3,
            "added_this_week": 12,
            "added_this_month": 30,
            "avg_trust_score": 0.85,
            "last_registration_at": None,
        })
        result = await get_registration_stats(pool, 123)
        assert result["total"] == 50
        assert result["active"] == 40
        assert result["inactive"] == 8
        assert result["banned"] == 2
        assert result["added_today"] == 3
        assert result["added_this_week"] == 12
        assert result["added_this_month"] == 30
        assert result["avg_trust_score"] == 0.85

    @pytest.mark.asyncio
    async def test_registration_stats_zero_accounts(self):
        from bot.handlers.auto_registrar import get_registration_stats
        pool = FakePool(fetch_row={
            "total": 0,
            "active": 0,
            "inactive": 0,
            "banned": 0,
            "added_today": 0,
            "added_this_week": 0,
            "added_this_month": 0,
            "avg_trust_score": None,
            "last_registration_at": None,
        })
        result = await get_registration_stats(pool, 123)
        assert result["total"] == 0
        assert result["active"] == 0
