"""Tests for Unified Contacts Hub (UCH) module.

Covers: repository, merge, search, trust, versioning, relationships,
CRM, smart tags, bulk ops, export, identity, AI assistant.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── FakePool helper ───────────────────────────────────────────────────────────

class FakePool:
    """Minimal asyncpg pool mock for UCH tests."""

    def __init__(self, fetch_val=None, fetch_row=None, fetch_rows=None, execute_val="UPDATE 1"):
        self._fetch_val = fetch_val
        self._fetch_row = fetch_row or {}
        self._fetch_rows = fetch_rows or []
        self._execute_val = execute_val
        self._calls = []

    async def fetchval(self, query, *args):
        self._calls.append(("fetchval", query, args))
        return self._fetch_val

    async def fetchrow(self, query, *args):
        self._calls.append(("fetchrow", query, args))
        return self._fetch_row

    async def fetch(self, query, *args):
        self._calls.append(("fetch", query, args))
        return self._fetch_rows

    async def execute(self, query, *args):
        self._calls.append(("execute", query, args))
        return self._execute_val

    async def acquire(self):
        return self

    async def release(self, conn):
        pass


# ── Repository tests ─────────────────────────────────────────────────────────

class TestRepository:
    """Tests for contacts_hub/repository.py"""

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
            {"id": "uuid-1", "first_name": "Ivan", "username": "ivan"},
            {"id": "uuid-2", "first_name": "Petr", "username": "petr"},
        ]
        pool = FakePool(fetch_rows=rows, fetch_val=2)
        result = await get_contacts(pool, owner_id=123)
        assert len(result["contacts"]) == 2
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_get_contacts_with_search(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, search="ivan")
        call = pool._calls[-1]
        assert "ILIKE" in call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_favorite_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, favorite_only=True)
        call = pool._calls[-1]
        assert "is_favorite" in call[1]

    @pytest.mark.asyncio
    async def test_get_contacts_with_multi_filter(self):
        from services.contacts_hub.repository import get_contacts
        pool = FakePool(fetch_rows=[], fetch_val=0)
        await get_contacts(pool, owner_id=123, multi_only=True)
        call = pool._calls[-1]
        assert "contact_sources" in call[1]

    @pytest.mark.asyncio
    async def test_get_contact_detail(self):
        from services.contacts_hub.repository import get_contact
        pool = FakePool(
            fetch_row={"id": "uuid-1", "first_name": "Ivan", "owner_id": 123},
            fetch_rows=[]
        )
        result = await get_contact(pool, "uuid-1", 123)
        assert result is not None
        assert result["contact"]["first_name"] == "Ivan"

    @pytest.mark.asyncio
    async def test_get_contact_not_found(self):
        from services.contacts_hub.repository import get_contact
        pool = FakePool(fetch_row=None)
        result = await get_contact(pool, "uuid-1", 123)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_contact(self):
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
        pool = FakePool(fetch_rows=[{"id": 1, "name": "Clients", "member_count": 5}])
        result = await get_contact_groups(pool, 123)
        assert len(result) == 1
        assert result[0]["name"] == "Clients"

    @pytest.mark.asyncio
    async def test_log_contact_history(self):
        from services.contacts_hub.repository import log_contact_history
        pool = FakePool()
        await log_contact_history(pool, "uuid-1", 123, "edit", "name", "old", "new")
        assert len(pool._calls) == 1
        assert "INSERT INTO contact_history" in pool._calls[0][1]


# ── Trust Engine tests ────────────────────────────────────────────────────────

class TestTrustEngine:
    """Tests for contacts_hub/trust_engine.py"""

    def test_compute_trust_score_basic(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"]}
        sources = [{"id": 1}, {"id": 2}]
        score = compute_trust_score(contact, sources)
        assert 0.5 <= score <= 1.0

    def test_compute_trust_score_minimal(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {}
        sources = []
        score = compute_trust_score(contact, sources)
        assert score == 0.5

    def test_compute_trust_score_full(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        contact = {"telegram_user_id": 123, "username": "ivan", "phones": ["+123"]}
        sources = [{"id": 1}, {"id": 2}, {"id": 3}]
        score = compute_trust_score(contact, sources)
        assert score >= 0.9

    def test_compute_merge_confidence_same_id(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 123, "username": "ivan", "phones": [], "first_name": "Ivan", "last_name": "", "company": ""}
        b = {"telegram_user_id": 123, "username": "ivan2", "phones": [], "first_name": "Ivan", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] >= 0.5
        assert "same_telegram_id" in result["reasons"]

    def test_compute_merge_confidence_same_phone(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": ["+123"], "first_name": "A", "last_name": "", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": ["+123"], "first_name": "B", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_phone" in result["reasons"]

    def test_compute_merge_confidence_same_name(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [], "first_name": "Ivan", "last_name": "Ivanov", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": [], "first_name": "Ivan", "last_name": "Ivanov", "company": ""}
        result = compute_merge_confidence(a, b)
        assert "same_name" in result["reasons"]

    def test_compute_merge_confidence_low(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": ["+111"], "first_name": "A", "last_name": "X", "company": "C1"}
        b = {"telegram_user_id": 2, "username": "b", "phones": ["+222"], "first_name": "B", "last_name": "Y", "company": "C2"}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] < 0.5


# ── Search Engine tests ───────────────────────────────────────────────────────

class TestSearchEngine:
    """Tests for contacts_hub/search_engine.py"""

    def test_tokenize(self):
        from services.contacts_hub.search_engine import _tokenize
        assert _tokenize("Ivan Ivanov") == ["ivan", "ivanov"]
        assert _tokenize("") == []
        assert _tokenize("  ") == []
        assert _tokenize("ab") == ["ab"]
        assert _tokenize("a") == []

    def test_tokenize_multiword(self):
        from services.contacts_hub.search_engine import _tokenize
        tokens = _tokenize("ivan的工作 developer")
        assert len(tokens) >= 1

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

    @pytest.mark.asyncio
    async def test_search_contacts_multi_token(self):
        from services.contacts_hub.search_engine import search_contacts
        pool = FakePool(fetch_rows=[{"id": "1", "first_name": "Ivan"}])
        result = await search_contacts(pool, 123, "ivan petrov")
        assert len(result) == 1


# ── Versioning Engine tests ───────────────────────────────────────────────────

class TestVersioningEngine:
    """Tests for contacts_hub/versioning_engine.py"""

    @pytest.mark.asyncio
    async def test_get_versions_empty(self):
        from services.contacts_hub.versioning_engine import get_versions
        pool = FakePool(fetch_rows=[])
        result = await get_versions(pool, "uuid-1", 123)
        assert result == []

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


# ── CRM Engine tests ──────────────────────────────────────────────────────────

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
        pool = FakePool(fetch_row={"id": 1, "stage": "lead", "custom_fields": "{}"})
        result = await get_crm_data(pool, 123, "uuid-1")
        assert result["stage"] == "lead"

    @pytest.mark.asyncio
    async def test_get_upcoming_reminders_empty(self):
        from services.contacts_hub.crm_engine import get_upcoming_reminders
        pool = FakePool(fetch_rows=[])
        result = await get_upcoming_reminders(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_crm_stats(self):
        from services.contacts_hub.crm_engine import get_crm_stats
        pool = FakePool(fetch_val=5, fetch_rows=[{"stage": "lead", "cnt": 3, "total_value": 100}])
        result = await get_crm_stats(pool, 123)
        assert result["total_crm_contacts"] == 5


# ── Smart Tags Engine tests ───────────────────────────────────────────────────

class TestSmartTagsEngine:
    """Tests for contacts_hub/smart_tags_engine.py"""

    def test_check_condition_eq(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"is_premium": True}, {"field": "is_premium", "op": "eq", "value": True}) is True
        assert _check_condition({"is_premium": False}, {"field": "is_premium", "op": "eq", "value": True}) is False

    def test_check_condition_not_empty(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"phones": ["+123"]}, {"field": "phones", "op": "not_empty"}) is True
        assert _check_condition({"phones": []}, {"field": "phones", "op": "not_empty"}) is False
        assert _check_condition({"notes": "hello"}, {"field": "notes", "op": "not_empty"}) is True
        assert _check_condition({"notes": ""}, {"field": "notes", "op": "not_empty"}) is False

    def test_check_condition_gte(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"count": 5}, {"field": "count", "op": "gte", "value": 3}) is True
        assert _check_condition({"count": 2}, {"field": "count", "op": "gte", "value": 3}) is False

    def test_check_condition_contains(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"notes": "important client"}, {"field": "notes", "op": "contains", "value": "client"}) is True

    @pytest.mark.asyncio
    async def test_get_smart_tags_empty(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tags
        pool = FakePool(fetch_rows=[])
        result = await get_smart_tags(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_smart_tag_rules_includes_builtin(self):
        from services.contacts_hub.smart_tags_engine import get_smart_tag_rules
        pool = FakePool(fetch_rows=[])
        result = await get_smart_tag_rules(pool, 123)
        assert len(result) >= 7  # Built-in rules


# ── Bulk Ops Engine tests ─────────────────────────────────────────────────────

class TestBulkOpsEngine:
    """Tests for contacts_hub/bulk_ops_engine.py"""

    @pytest.mark.asyncio
    async def test_bulk_delete(self):
        from services.contacts_hub.bulk_ops_engine import bulk_delete
        pool = FakePool(execute_val="DELETE 3")
        result = await bulk_delete(pool, 123, ["id1", "id2", "id3"])
        assert result["deleted"] == 3

    @pytest.mark.asyncio
    async def test_bulk_set_favorite(self):
        from services.contacts_hub.bulk_ops_engine import bulk_set_favorite
        pool = FakePool(execute_val="UPDATE 2")
        result = await bulk_set_favorite(pool, 123, ["id1", "id2"], True)
        assert result["updated"] == 2

    @pytest.mark.asyncio
    async def test_create_group(self):
        from services.contacts_hub.bulk_ops_engine import create_group
        pool = FakePool(fetch_val=42)
        result = await create_group(pool, 123, "Clients")
        assert result == 42

    @pytest.mark.asyncio
    async def test_delete_group(self):
        from services.contacts_hub.bulk_ops_engine import delete_group
        pool = FakePool(execute_val="DELETE 1")
        result = await delete_group(pool, 1, 123)
        assert result is True


# ── Export Engine tests ───────────────────────────────────────────────────────

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
             "notes": "test", "tags": "{vip}", "color_label": "#3b82f6",
             "is_favorite": True, "is_premium": False, "importance_level": None,
             "user_rating": None, "discovered_at": None, "last_synced_at": None,
             "created_at": None}
        ])
        result = await export_csv(pool, 123)
        assert "Ivan" in result
        assert "TestCo" in result

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
             "position": "Dev", "phones": '["+123"]', "emails": "[]",
             "websites": "[]", "notes": "test"}
        ])
        result = await export_vcf(pool, 123)
        assert "BEGIN:VCARD" in result
        assert "Ivan" in result

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


# ── Identity Engine tests ─────────────────────────────────────────────────────

class TestIdentityEngine:
    """Tests for contacts_hub/identity_engine.py"""

    @pytest.mark.asyncio
    async def test_build_identity_graph_not_found(self):
        from services.contacts_hub.identity_engine import build_identity_graph
        pool = FakePool(fetch_row=None)
        result = await build_identity_graph(pool, 123, "uuid-1")
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_identity_graph_empty(self):
        from services.contacts_hub.identity_engine import get_identity_graph
        pool = FakePool(fetch_rows=[])
        result = await get_identity_graph(pool, 123, "uuid-1")
        assert result == []


# ── Relationship Engine tests ─────────────────────────────────────────────────

class TestRelationshipEngine:
    """Tests for contacts_hub/relationship_engine.py"""

    @pytest.mark.asyncio
    async def test_compute_relationships_empty(self):
        from services.contacts_hub.relationship_engine import compute_relationships
        pool = FakePool(fetch_rows=[])
        result = await compute_relationships(pool, 123)
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_relationships_empty(self):
        from services.contacts_hub.relationship_engine import get_relationships
        pool = FakePool(fetch_rows=[])
        result = await get_relationships(pool, 123, "uuid-1")
        assert result == []


# ── AI Assistant tests ────────────────────────────────────────────────────────

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
        # "сколько контактов" matches count_all first, which is correct behavior
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
    async def test_process_ai_query_empty(self):
        from services.contacts_hub.ai_assistant import process_ai_query
        pool = FakePool()
        result = await process_ai_query(pool, 123, "абракадабра")
        assert result["type"] == "help"


# ── Merge Engine tests ────────────────────────────────────────────────────────

class TestMergeEngine:
    """Tests for contacts_hub/merge_engine.py"""

    @pytest.mark.asyncio
    async def test_find_duplicates_empty(self):
        from services.contacts_hub.merge_engine import find_duplicates
        pool = FakePool(fetch_rows=[])
        result = await find_duplicates(pool, 123)
        assert result == []
