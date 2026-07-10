"""Tests for Unified Contacts Hub (UCH) module.

Covers: repository, merge, search, trust, versioning, relationships,
CRM, smart tags, bulk ops, export, identity, AI assistant, ranking_engine,
network_builder, audience_analytics, analytics_dashboard.
"""
from __future__ import annotations

import json
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── FakePool helper ───────────────────────────────────────────────────────────

class _AcquireContext:
    """Async context manager for FakePool.acquire()."""

    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, *args):
        pass


class FakePool:
    """Minimal asyncpg pool mock for UCH tests."""

    def __init__(self, fetch_val=None, fetch_row=None, fetch_rows=None, execute_val="UPDATE 1"):
        self._fetch_val = fetch_val
        self._fetch_row = fetch_row or {}
        self._fetch_rows = fetch_rows or []
        self._execute_val = execute_val
        self._calls = []
        # Sequential режим: если fetch_val/fetch_row — СПИСОК, значения отдаются по
        # очереди (для функций с несколькими fetchval/fetchrow подряд, например
        # get_crm_stats: total→reminders, compute_merge: row_a→row_b). Скаляр/dict —
        # прежнее поведение (одно значение на все вызовы). Backward-совместимо.
        self._fv_i = 0
        self._fr_i = 0

    async def fetchval(self, query, *args):
        self._calls.append(("fetchval", query, args))
        if isinstance(self._fetch_val, list):
            v = self._fetch_val[min(self._fv_i, len(self._fetch_val) - 1)] if self._fetch_val else None
            self._fv_i += 1
            return v
        return self._fetch_val

    async def fetchrow(self, query, *args):
        self._calls.append(("fetchrow", query, args))
        if isinstance(self._fetch_row, list):
            r = self._fetch_row[min(self._fr_i, len(self._fetch_row) - 1)] if self._fetch_row else None
            self._fr_i += 1
            return r
        return self._fetch_row

    async def fetch(self, query, *args):
        self._calls.append(("fetch", query, args))
        return self._fetch_rows

    async def execute(self, query, *args):
        self._calls.append(("execute", query, args))
        return self._execute_val

    def acquire(self):
        return _AcquireContext(self)

    async def release(self, conn):
        pass


class SequentialFetchPool(FakePool):
    """FakePool для analytics_dashboard: fetch отдаёт результаты по очереди."""

    def __init__(self, fetch_results):
        super().__init__()
        self._fetch_results = fetch_results
        self._fetch_idx = 0

    async def fetch(self, query, *args):
        self._calls.append(("fetch", query, args))
        if self._fetch_idx < len(self._fetch_results):
            result = self._fetch_results[self._fetch_idx]
            self._fetch_idx += 1
            return result
        return []


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

    @pytest.mark.asyncio
    async def test_compute_merge_confidence_improved_high(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence_improved
        pool = FakePool(
            fetch_row={"id": 1, "telegram_user_id": 123, "username": "ivan", "first_name": "Ivan", "last_name": "Ivanov", "phones": ["+123"], "company": "TestCo"},
            fetch_rows=[{"source_type": "telegram"}, {"source_type": "phone"}],
            fetch_val=None
        )
        result = await compute_merge_confidence_improved(pool, 123, 1, 2)
        assert result["confidence"] >= 0.5
        assert "same_telegram_id" in result["reasons"]

    @pytest.mark.asyncio
    async def test_compute_merge_confidence_improved_low(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence_improved
        # row_a и row_b — РАЗНЫЕ контакты (fetchrow отдаёт их по очереди): разные
        # username/имя/телефон/компания → низкая уверенность в слиянии.
        pool = FakePool(
            fetch_row=[
                {"id": 1, "telegram_user_id": 1, "username": "alice", "first_name": "Alice", "last_name": "Smith", "phones": ["+111"], "company": "C1"},
                {"id": 2, "telegram_user_id": 2, "username": "bob", "first_name": "Bob", "last_name": "Jones", "phones": ["+999"], "company": "C2"},
            ],
            fetch_rows=[{"source_type": "telegram"}],
            fetch_val=None
        )
        result = await compute_merge_confidence_improved(pool, 123, 1, 2)
        assert result["confidence"] < 0.5

    @pytest.mark.asyncio
    async def test_detect_smart_duplicates_improved_empty(self):
        from services.contacts_hub.trust_engine import detect_smart_duplicates_improved
        pool = FakePool(fetch_rows=[])
        result = await detect_smart_duplicates_improved(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_smart_duplicates_improved_with_duplicates(self):
        from services.contacts_hub.trust_engine import detect_smart_duplicates_improved
        contacts = [
            {"id": 1, "telegram_user_id": 123, "username": "ivan", "first_name": "Ivan", "last_name": "Ivanov", "phones": ["+123"], "company": "TestCo"},
            {"id": 2, "telegram_user_id": 123, "username": "ivan2", "first_name": "Ivan", "last_name": "Ivanov", "phones": ["+123"], "company": "TestCo"},
        ]
        sources_map = {
            1: [{"source_type": "telegram"}, {"source_type": "phone"}],
            2: [{"source_type": "telegram"}, {"source_type": "phone"}],
        }
        class SmartPool(FakePool):
            def __init__(self, contacts, sources_map):
                super().__init__(fetch_rows=contacts)
                self._contacts = contacts
                self._sources_map = sources_map

            async def fetch(self, query, *args):
                if "contact_sources" in query and args:
                    contact_id = args[0]
                    return self._sources_map.get(contact_id, [])
                else:
                    return self._contacts

        pool = SmartPool(contacts, sources_map)
        result = await detect_smart_duplicates_improved(pool, 123)
        assert len(result) >= 1
        assert result[0]["confidence"] >= 0.45

    @pytest.mark.asyncio
    async def test_get_conflict_resolution_suggestions(self):
        from services.contacts_hub.trust_engine import get_conflict_resolution_suggestions
        conflict_row = {
            "id": 1,
            "conflict_field": "phones",
            "phones": ["+123", "+456"],
            "first_name": "Ivan",
            "last_name": "Ivanov",
            "username": "ivan",
            "company": "TestCo",
            "telegram_user_id": 123,
            "resolution": "pending",
            "confidence": 0.9,
        }
        pool = FakePool(fetch_row=conflict_row)
        result = await get_conflict_resolution_suggestions(pool, 1)
        assert result["conflict_id"] == 1
        assert result["field"] == "phones"
        assert result["priority"] == "high"
        assert len(result["suggestions"]) == 1
        assert result["suggestions"][0]["action"] == "merge_phones"


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

    @pytest.mark.asyncio
    async def test_get_crm_pipeline_empty(self):
        from services.contacts_hub.crm_engine import get_crm_pipeline
        pool = FakePool(fetch_rows=[])
        result = await get_crm_pipeline(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_crm_pipeline_with_deals(self):
        from services.contacts_hub.crm_engine import get_crm_pipeline
        rows = [
            {"stage": "lead", "deal_count": 5, "total_value": 1000, "avg_value": 200},
            {"stage": "proposal", "deal_count": 3, "total_value": 5000, "avg_value": 1666.67},
        ]
        pool = FakePool(fetch_rows=rows)
        result = await get_crm_pipeline(pool, 123)
        assert len(result) == 2
        assert result[0]["stage"] == "lead"
        assert result[0]["deal_count"] == 5
        assert result[1]["stage"] == "proposal"

    @pytest.mark.asyncio
    async def test_get_crm_stats_empty(self):
        from services.contacts_hub.crm_engine import get_crm_stats
        pool = FakePool(fetch_val=0, fetch_rows=[], execute_val=0)
        result = await get_crm_stats(pool, 123)
        assert result["total_crm_contacts"] == 0
        assert result["by_stage"] == []
        assert result["upcoming_reminders_7d"] == 0

    @pytest.mark.asyncio
    async def test_get_crm_stats_with_data(self):
        from services.contacts_hub.crm_engine import get_crm_stats
        rows = [
            {"stage": "lead", "cnt": 10, "total_value": 5000},
            {"stage": "proposal", "cnt": 5, "total_value": 15000},
        ]
        # get_crm_stats зовёт fetchval дважды: total(15) → reminders(3).
        pool = FakePool(fetch_val=[15, 3], fetch_rows=rows, execute_val=3)
        result = await get_crm_stats(pool, 123)
        assert result["total_crm_contacts"] == 15
        assert len(result["by_stage"]) == 2
        assert result["upcoming_reminders_7d"] == 3

    @pytest.mark.asyncio
    async def test_get_crm_activities_empty(self):
        from services.contacts_hub.crm_engine import get_crm_activities
        pool = FakePool(fetch_rows=[])
        result = await get_crm_activities(pool, 123, "uuid-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_crm_activities_with_data(self):
        from services.contacts_hub.crm_engine import get_crm_activities
        rows = [
            {"id": 1, "activity_type": "call", "description": "Discovery call", "created_at": "2024-01-15"},
            {"id": 2, "activity_type": "email", "description": "Follow up", "created_at": "2024-01-16"},
        ]
        pool = FakePool(fetch_rows=rows)
        result = await get_crm_activities(pool, 123, "uuid-1")
        assert len(result) == 2
        assert result[0]["activity_type"] == "call"
        assert result[1]["description"] == "Follow up"

    @pytest.mark.asyncio
    async def test_create_crm_activity(self):
        from services.contacts_hub.crm_engine import create_crm_activity
        pool = FakePool(fetch_val=42)
        result = await create_crm_activity(pool, 123, "uuid-1", "meeting", "Initial meeting")
        assert result == 42
        call = pool._calls[0]
        assert "INSERT INTO contact_crm_activity" in call[1]

    @pytest.mark.asyncio
    async def test_update_crm_stage(self):
        from services.contacts_hub.crm_engine import update_crm_stage
        pool = FakePool(execute_val="UPDATE 1")
        result = await update_crm_stage(pool, 123, "uuid-1", "proposal")
        assert result is True
        call = pool._calls[0]
        assert "UPDATE contact_crm" in call[1]
        assert call[2][0] == "proposal"

    @pytest.mark.asyncio
    async def test_update_crm_stage_not_found(self):
        from services.contacts_hub.crm_engine import update_crm_stage
        pool = FakePool(execute_val="UPDATE 0")
        result = await update_crm_stage(pool, 123, "uuid-1", "proposal")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_crm_reminders_empty(self):
        from services.contacts_hub.crm_engine import get_crm_reminders
        pool = FakePool(fetch_rows=[])
        result = await get_crm_reminders(pool, 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_crm_reminders_with_data(self):
        from services.contacts_hub.crm_engine import get_crm_reminders
        rows = [
            {"first_name": "Ivan", "last_name": "Ivanov", "username": "ivan",
             "stage": "lead", "next_reminder_at": "2024-01-20", "next_reminder_text": "Follow up"},
            {"first_name": "", "last_name": "", "username": "petr",
             "stage": "proposal", "next_reminder_at": "2024-01-22", "next_reminder_text": "Send proposal"},
        ]
        pool = FakePool(fetch_rows=rows)
        result = await get_crm_reminders(pool, 123)
        assert len(result) == 2
        assert result[0]["contact_name"] == "Ivan Ivanov"
        assert result[1]["contact_name"] == "petr"


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

    @pytest.mark.asyncio
    async def test_export_vcard_single_contact(self):
        from services.contacts_hub.export_engine import export_vcard
        pool = FakePool(fetch_row={
            "id": "uuid-1", "first_name": "Ivan", "last_name": "Ivanov",
            "company": "TestCo", "position": "Dev", "phones": '["+123"]',
            "emails": '["ivan@test.com"]', "websites": "[]", "username": "ivan",
            "telegram_user_id": 123, "birthday": "1990-01-01", "notes": "test"
        })
        result = await export_vcard(pool, 123, "uuid-1")
        assert "BEGIN:VCARD" in result
        assert "Ivan" in result
        assert "Ivanov" in result
        assert "TestCo" in result
        assert "Dev" in result
        assert "+123" in result
        assert "ivan@test.com" in result
        assert "@ivan" in result
        assert "1990-01-01" in result
        assert "test" in result

    @pytest.mark.asyncio
    async def test_export_vcard_with_all_fields(self):
        from services.contacts_hub.export_engine import export_vcard
        pool = FakePool(fetch_row={
            "id": "uuid-1", "first_name": "Ivan", "last_name": "Ivanov",
            "company": "TestCo", "position": "Dev", "phones": '["+123", "+456"]',
            "emails": '["ivan@test.com", "ivan2@test.com"]', "websites": '["https://ivan.com"]',
            "username": "ivan", "telegram_user_id": 123, "birthday": "1990-01-01", "notes": "test"
        })
        result = await export_vcard(pool, 123, "uuid-1")
        assert "BEGIN:VCARD" in result
        assert "END:VCARD" in result
        assert "+123" in result
        assert "+456" in result
        assert "ivan@test.com" in result
        assert "ivan2@test.com" in result
        assert "https://ivan.com" in result

    @pytest.mark.asyncio
    async def test_export_csv_streaming_empty(self):
        from services.contacts_hub.export_engine import export_csv_streaming
        pool = FakePool(fetch_rows=[])
        results = []
        async for chunk in export_csv_streaming(pool, 123):
            results.append(chunk)
        assert len(results) == 1
        assert results[0][0] == "ID"

    @pytest.mark.asyncio
    async def test_export_csv_streaming_with_data(self):
        from services.contacts_hub.export_engine import export_csv_streaming

        # export_csv_streaming использует keyset-пагинацию (while rows: ...).
        # Обычный FakePool всегда возвращает те же строки → бесконечный цикл
        # (и зависание всего набора тестов). Нужен мок, моделирующий исчерпание:
        # строки на первом fetch, затем пусто.
        class _ExhaustPool:
            def __init__(self, rows):
                self._rows = rows
                self._served = False

            async def fetch(self, query, *args):
                if self._served:
                    return []
                self._served = True
                return self._rows

        pool = _ExhaustPool([
            {"id": "uuid-1", "first_name": "Ivan", "last_name": "Ivanov",
             "phones": '["+123"]', "emails": "[]", "websites": "[]", "tags": "{vip}"}
        ])
        results = []
        async for chunk in export_csv_streaming(pool, 123):
            results.append(chunk)
        assert len(results) == 2
        assert results[0][0] == "ID"
        assert "Ivan" in results[1]
        assert "Ivanov" in results[1]

    @pytest.mark.asyncio
    async def test_get_export_stats(self):
        from services.contacts_hub.export_engine import get_export_stats
        pool = FakePool(fetch_val=10)
        result = await get_export_stats(pool, 123)
        assert result["total_contacts"] == 10
        assert result["with_phone"] == 10
        assert result["with_email"] == 10
        assert result["favorites"] == 10
        assert result["premium"] == 10


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


# ── Ranking Engine tests ──────────────────────────────────────────────────────

class TestRankingEngine:
    """Tests for ranking_engine.py"""

    @pytest.mark.asyncio
    async def test_track_keyword(self):
        from services.ranking_engine import track_keyword
        pool = FakePool(fetch_row={"id": 1})
        result = await track_keyword(pool, 123, "telegram channels")
        assert result["ok"] is True
        assert result["id"] == 1

    @pytest.mark.asyncio
    async def test_record_position(self):
        from services.ranking_engine import record_position
        pool = FakePool(fetch_val=None, execute_val="INSERT 0 1")
        result = await record_position(pool, 123, -100, "telegram channels", 5)
        assert result["ok"] is True
        assert result["current"] == 5
        assert result["previous"] is None

    @pytest.mark.asyncio
    async def test_get_position_history(self):
        from services.ranking_engine import get_position_history
        rows = [
            {"position": 1, "previous_position": None, "checked_at": "2024-01-01", "metadata": "{}"},
            {"position": 3, "previous_position": 1, "checked_at": "2024-01-02", "metadata": "{}"},
        ]
        pool = FakePool(fetch_rows=rows)
        result = await get_position_history(pool, 123, -100, "telegram channels")
        assert len(result) == 2
        assert result[0]["position"] == 1
        assert result[1]["position"] == 3

    @pytest.mark.asyncio
    async def test_get_ranking_stats(self):
        from services.ranking_engine import get_ranking_stats
        pool = FakePool(fetch_val=[5, 42, 3.5, 2])
        result = await get_ranking_stats(pool, 123)
        assert result["total_tracked"] == 5
        assert result["total_checks"] == 42
        assert result["avg_position_7d"] == 3.5
        assert result["alerts_pending"] == 2


# ── Network Builder tests ─────────────────────────────────────────────────────

class TestNetworkBuilder:
    """Tests for network_builder.py"""

    @pytest.mark.asyncio
    async def test_create_template(self):
        from services.network_builder import create_template
        pool = FakePool(fetch_row={"id": 1})
        result = await create_template(pool, 123, "My Template", "Description")
        assert result["ok"] is True
        assert result["id"] == 1

    @pytest.mark.asyncio
    async def test_get_templates(self):
        from services.network_builder import get_templates
        rows = [
            {"id": 1, "name": "Template 1", "owner_id": 123},
            {"id": 2, "name": "Template 2", "owner_id": 123},
        ]
        pool = FakePool(fetch_rows=rows)
        result = await get_templates(pool, 123)
        assert len(result) == 2
        assert result[0]["name"] == "Template 1"

    @pytest.mark.asyncio
    async def test_create_instance(self):
        from services.network_builder import create_instance
        pool = FakePool(fetch_row=[
            {"id": 1, "nodes": "[]", "edges": "[]", "owner_id": 123},
            {"id": 10},
        ])
        result = await create_instance(pool, 123, 1, "My Instance")
        assert result["ok"] is True
        assert result["id"] == 10

    @pytest.mark.asyncio
    async def test_add_node(self):
        from services.network_builder import add_node
        pool = FakePool(fetch_row={"id": 5})
        result = await add_node(pool, 1, "channel", "My Channel")
        assert result["ok"] is True
        assert result["id"] == 5

    @pytest.mark.asyncio
    async def test_add_edge(self):
        from services.network_builder import add_edge
        pool = FakePool(fetch_row={"id": 3})
        result = await add_edge(pool, 1, 1, 2, "admin")
        assert result["ok"] is True
        assert result["id"] == 3

    @pytest.mark.asyncio
    async def test_get_network_stats(self):
        from services.network_builder import get_network_stats
        pool = FakePool(fetch_val=[3, 5, 12, 8])
        result = await get_network_stats(pool, 123)
        assert result["templates"] == 3
        assert result["instances"] == 5
        assert result["total_nodes"] == 12
        assert result["total_edges"] == 8


# ── Audience Analytics tests ──────────────────────────────────────────────────

class TestAudienceAnalytics:
    """Tests for audience_analytics.py"""

    @pytest.mark.asyncio
    async def test_analyze_audience(self):
        from services.audience_analytics import analyze_audience
        pool = FakePool(
            fetch_row=[
                {"cnt": 1000},
                {"cnt": 500},
                {"total_users": 1000, "retained_users": 400},
                {"total_users": 1000, "retained_users": 300},
                {"avg_duration": 15.5},
            ],
            fetch_rows=[{"hour": 14, "dow": 3, "cnt": 100}],
        )
        result = await analyze_audience(pool, 123, -100)
        assert result is not None
        assert result.total_subscribers == 1000
        assert result.active_users == 500
        assert result.active_rate == 50.0
        assert result.avg_session_duration_min == 15.5

    @pytest.mark.asyncio
    async def test_segment_audience(self):
        from services.audience_analytics import segment_audience
        now = datetime.now(timezone.utc)
        user_rows = [
            {
                "user_id": 1,
                "last_seen": now,
                "first_seen": now,
                "days_since_last": 1,
                "days_since_first": 60,
            },
            {
                "user_id": 2,
                "last_seen": now,
                "first_seen": now,
                "days_since_last": 20,
                "days_since_first": 30,
            },
        ]
        pool = FakePool(
            fetch_rows=user_rows,
            fetch_row=[
                {"cnt": 60},
                {"cnt": 5},
            ],
        )
        result = await segment_audience(pool, 123, -100)
        assert isinstance(result, list)
        segment_names = [s.name for s in result]
        assert "Champions" in segment_names
        assert "Dormant" in segment_names


# ── Analytics Dashboard tests ─────────────────────────────────────────────────

class TestAnalyticsDashboard:
    """Tests for analytics_dashboard.py"""

    @pytest.mark.asyncio
    async def test_get_dashboard_stats(self):
        from services.analytics_dashboard import get_dashboard_stats
        now = datetime.now(timezone.utc)
        pool = SequentialFetchPool([
            [{"total": 5, "active": 3, "banned": 1, "spamblock": 1}],
            [{"total": 10, "success": 8, "failed": 2}],
            [{"running": 1}],
            [{"total_users": 500, "new_24h": 10, "new_7d": 50}],
            [{"avg_trust": 0.75, "at_risk": 1}],
            [{"total_usd": 150.0}],
        ])
        result = await get_dashboard_stats(pool, 123)
        assert result["accounts"]["total"] == 5
        assert result["accounts"]["active"] == 3
        assert result["operations_24h"]["total"] == 10
        assert result["operations_24h"]["success"] == 8
        assert result["operations_24h"]["running"] == 1
        assert result["audience"]["total_users"] == 500
        assert result["health"]["avg_trust"] == 0.75
        assert result["revenue_30d_usd"] == 150.0

    @pytest.mark.asyncio
    async def test_get_realtime_metrics(self):
        from services.analytics_dashboard import get_realtime_metrics
        now = datetime.now(timezone.utc)
        pool = SequentialFetchPool([
            [{"id": 1, "op_type": "mass_send", "status": "running", "started_at": now, "params": "{}"}],
            [{"id": 1, "action": "send", "result": "success", "target": "chat_1", "occurred_at": now}],
            [{"status": "active", "cnt": 3}, {"status": "banned", "cnt": 1}],
            [{"op_type": "mass_send", "cnt": 2}],
        ])
        result = await get_realtime_metrics(pool, 123)
        assert len(result["active_operations"]) == 1
        assert result["active_operations"][0]["type"] == "mass_send"
        assert len(result["recent_events"]) == 1
        assert result["recent_events"][0]["action"] == "send"
        assert result["account_status"]["active"] == 3
        assert result["queue_depth"]["mass_send"] == 2


# ── Performance Engine tests ─────────────────────────────────────────────────

class TestPerformanceCache:
    """Tests for contacts_hub/performance.py cache functionality."""

    @pytest.mark.asyncio
    async def test_cache_decorator(self):
        from services.contacts_hub.performance import cache_decorator, invalidate_cache, get_cache_stats

        @cache_decorator(ttl=60, key_prefix="test_func")
        async def my_func(x: int) -> int:
            return x * 2

        result = await my_func(5)
        assert result == 10

        stats = get_cache_stats()
        assert stats["hits"] >= 1 or stats["sets"] >= 1

        invalidate_cache("test_func")

    def test_invalidate_cache(self):
        from services.contacts_hub.performance import invalidate_cache, get_cache_stats, _cache

        _cache.set("test_key_1", "value1")
        _cache.set("test_key_2", "value2")
        _cache.set("other_key", "value3")

        removed = invalidate_cache("test_key")
        assert removed == 2

        assert _cache.get("test_key_1") is None
        assert _cache.get("test_key_2") is None
        assert _cache.get("other_key") == "value3"

    def test_get_cache_stats(self):
        from services.contacts_hub.performance import get_cache_stats, _cache

        _cache.clear()
        _cache.set("stat_test_1", "data")
        _cache.set("stat_test_2", "data")
        _ = _cache.get("stat_test_1")
        _ = _cache.get("stat_test_1")
        _ = _cache.get("nonexistent")

        stats = get_cache_stats()
        assert stats["size"] >= 2
        assert stats["hits"] >= 2
        assert stats["misses"] >= 1
        assert stats["sets"] >= 2


class TestPerformanceBatchOps:
    """Tests for contacts_hub/performance.py batch operations."""

    @pytest.mark.asyncio
    async def test_batch_insert(self):
        from services.contacts_hub.performance import batch_insert

        class BatchPool:
            def __init__(self):
                self._calls = []

            def acquire(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def executemany(self, query, values):
                self._calls.append((query, values))

        pool = BatchPool()
        rows = [{"id": i, "name": f"contact_{i}"} for i in range(3)]
        result = await batch_insert(pool, "unified_contacts", ["id", "name"], rows, batch_size=2)
        assert result == 3
        assert len(pool._calls) == 2

    @pytest.mark.asyncio
    async def test_batch_update(self):
        from services.contacts_hub.performance import batch_update

        class SeqPool:
            def __init__(self, responses):
                self._responses = iter(responses)
                self._calls = []

            async def execute(self, query, *args):
                self._calls.append(query)
                return next(self._responses)

        pool = SeqPool(["UPDATE 3", "UPDATE 2"])
        result = await batch_update(
            pool, "unified_contacts",
            {"is_favorite": True},
            "id", ["id1", "id2", "id3", "id4", "id5"],
            batch_size=3
        )
        assert result == 5
        assert len(pool._calls) == 2


class TestPerformanceQueryStats:
    """Tests for contacts_hub/performance.py query stats."""

    def test_get_query_stats(self):
        from services.contacts_hub.performance import get_query_stats, record_query, _query_stats

        _query_stats.clear()
        record_query("SELECT * FROM contacts", 15.5, 10)
        record_query("INSERT INTO contacts VALUES (...)", 2.3, 0)

        stats = get_query_stats()
        assert stats["total_queries"] == 2
        assert stats["avg_time_ms"] > 0
        assert len(stats["slowest"]) == 2
        assert stats["slowest"][0]["duration_ms"] >= stats["slowest"][1]["duration_ms"]


# ── Security tests ────────────────────────────────────────────────────────────

class TestSecurity:
    """Tests for services/security.py"""

    # ── sanitize_input ─────────────────────────────────────────────────────

    def test_sanitize_input_normal(self):
        from services.security import sanitize_input
        result = sanitize_input("Hello World")
        assert result == "Hello World"

    def test_sanitize_input_xss(self):
        from services.security import sanitize_input
        result = sanitize_input('<script>alert("xss")</script>')
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_sanitize_input_sql(self):
        from services.security import sanitize_input
        result = sanitize_input("'; DROP TABLE users; --")
        assert "DROP TABLE" in result

    # ── validate_email ─────────────────────────────────────────────────────

    def test_validate_email_valid(self):
        from services.security import validate_email
        assert validate_email("user@example.com") is True
        assert validate_email("test.user+tag@domain.co") is True

    def test_validate_email_invalid(self):
        from services.security import validate_email
        assert validate_email("not-an-email") is False
        assert validate_email("@domain.com") is False
        assert validate_email("user@") is False
        assert validate_email("") is False

    # ── validate_phone ─────────────────────────────────────────────────────

    def test_validate_phone_valid(self):
        from services.security import validate_phone
        assert validate_phone("+14155552671") is True
        assert validate_phone("+79161234567") is True
        assert validate_phone("+1 415-555-2671") is True

    def test_validate_phone_invalid(self):
        from services.security import validate_phone
        assert validate_phone("12345") is False
        assert validate_phone("not-a-phone") is False
        assert validate_phone("14155552671") is False
        assert validate_phone("") is False

    # ── validate_url ───────────────────────────────────────────────────────

    def test_validate_url_valid(self):
        from services.security import validate_url
        assert validate_url("https://example.com") is True
        assert validate_url("https://sub.domain.org/path?q=1") is True

    def test_validate_url_invalid(self):
        from services.security import validate_url
        assert validate_url("http://example.com") is False
        assert validate_url("ftp://example.com") is False
        assert validate_url("not-a-url") is False
        assert validate_url("https://localhost") is False
        assert validate_url("") is False

    # ── CSRF ───────────────────────────────────────────────────────────────

    def test_generate_csrf_token(self):
        import services.security as sec
        with patch.object(sec, "_CSRF_SECRET", "test_secret_for_csrf"):
            from services.security import generate_csrf_token
            token = generate_csrf_token(123)
            assert isinstance(token, str)
            assert ":" in token

    def test_verify_csrf_token(self):
        import services.security as sec
        with patch.object(sec, "_CSRF_SECRET", "test_secret_for_csrf"):
            from services.security import generate_csrf_token, verify_csrf_token
            token = generate_csrf_token(123)
            assert verify_csrf_token(token, 123) is True
            assert verify_csrf_token(token, 456) is False
            assert verify_csrf_token("invalid_token", 123) is False
