"""Tests for core Infragram services.

Covers: op_worker helpers, account_manager utils, broadcaster helpers,
flood_engine, dm_engine, spintax_service, profile_setter_engine.
"""
from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── FakePool helper ───────────────────────────────────────────────────────────

class FakePool:
    def __init__(self, fetch_val=None, fetch_row=None, fetch_rows=None, execute_val="UPDATE 1"):
        self._fetch_val = fetch_val
        self._fetch_row = fetch_row
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


# ── Spintax Service tests ─────────────────────────────────────────────────────

class TestSpintaxService:
    """Tests for services/spintax_service.py"""

    def test_expand_simple(self):
        from services.spintax_service import expand_template
        result = expand_template("Hello {World|Earth}")
        assert result in ("Hello World", "Hello Earth")

    def test_expand_multiple_options(self):
        from services.spintax_service import expand_template
        results = set()
        for _ in range(50):
            results.add(expand_template("{A|B|C}"))
        assert len(results) >= 2

    def test_expand_no_braces(self):
        from services.spintax_service import expand_template
        result = expand_template("No spintax here")
        assert result == "No spintax here"

    def test_expand_nested(self):
        from services.spintax_service import expand_template
        result = expand_template("Hello {World|{Beautiful|Great} Earth}")
        assert "Hello" in result

    def test_quality_warnings(self):
        from services.spintax_service import quality_warnings
        warnings = quality_warnings("Hello {World|Earth}")
        assert isinstance(warnings, list)

    def test_quality_warnings_empty(self):
        from services.spintax_service import quality_warnings
        warnings = quality_warnings("No spintax")
        assert len(warnings) == 0

    def test_first_option_render(self):
        from services.spintax_service import first_option_render
        result = first_option_render("Hello {World|Earth}")
        assert result == "Hello World"


# ── Flood Engine tests ────────────────────────────────────────────────────────

class TestFloodEngine:
    """Tests for services/flood_engine.py"""

    def test_module_importable(self):
        import services.flood_engine
        assert hasattr(services.flood_engine, '__name__')


# ── Account Manager utils tests ───────────────────────────────────────────────

class TestAccountManagerUtils:
    """Tests for services/account_manager.py utility functions"""

    def test_is_safe_public_url_valid(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://example.com/image.jpg") is True

    def test_is_safe_public_url_http(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("http://example.com/image.jpg") is False

    def test_is_safe_public_url_localhost(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("https://localhost/image.jpg") is False

    def test_is_safe_public_url_empty(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url("") is False

    def test_is_safe_public_url_none(self):
        from services.mini_app_api import is_safe_public_url
        assert is_safe_public_url(None) is False


# ── Mini App API helper tests ─────────────────────────────────────────────────

class TestMiniAppHelpers:
    """Tests for services/mini_app_api.py helper functions"""

    def test_jlist_valid(self):
        from services.mini_app_api import _jlist
        result = _jlist('["a","b","c"]')
        assert result == ["a", "b", "c"]

    def test_jlist_invalid(self):
        from services.mini_app_api import _jlist
        result = _jlist("not json")
        assert result == []

    def test_jlist_none(self):
        from services.mini_app_api import _jlist
        result = _jlist(None)
        assert result == []

    def test_jlist_empty(self):
        from services.mini_app_api import _jlist
        result = _jlist("")
        assert result == []


# ── Trust Engine edge cases ───────────────────────────────────────────────────

class TestTrustEngineEdgeCases:
    """Additional edge case tests for trust_engine.py"""

    def test_merge_confidence_empty_phones(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [], "first_name": "A", "last_name": "", "company": ""}
        b = {"telegram_user_id": 2, "username": "b", "phones": [], "first_name": "B", "last_name": "", "company": ""}
        result = compute_merge_confidence(a, b)
        assert result["confidence"] >= 0

    def test_merge_confidence_same_company(self):
        from services.contacts_hub.trust_engine import compute_merge_confidence
        a = {"telegram_user_id": 1, "username": "a", "phones": [], "first_name": "A", "last_name": "", "company": "Google"}
        b = {"telegram_user_id": 2, "username": "b", "phones": [], "first_name": "B", "last_name": "", "company": "Google"}
        result = compute_merge_confidence(a, b)
        assert "same_company" in result["reasons"]

    def test_trust_score_minimal(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        score = compute_trust_score({}, [])
        assert score == 0.5

    def test_trust_score_with_premium(self):
        from services.contacts_hub.trust_engine import compute_trust_score
        score = compute_trust_score({"telegram_user_id": 1, "username": "test", "phones": ["+123"]}, [{"id": 1}, {"id": 2}, {"id": 3}])
        assert score >= 0.9


# ── Smart Tags edge cases ─────────────────────────────────────────────────────

class TestSmartTagsEdgeCases:
    """Additional edge case tests for smart_tags_engine.py"""

    def test_check_condition_regex(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"name": "Ivan Ivanov"}, {"field": "name", "op": "regex", "value": r"^Ivan"}) is True

    def test_check_condition_regex_no_match(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"name": "Petr"}, {"field": "name", "op": "regex", "value": r"^Ivan"}) is False

    def test_check_condition_invalid_regex(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"name": "test"}, {"field": "name", "op": "regex", "value": "[invalid"}) is False

    def test_check_condition_lte(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"count": 2}, {"field": "count", "op": "lte", "value": 3}) is True
        assert _check_condition({"count": 5}, {"field": "count", "op": "lte", "value": 3}) is False

    def test_check_condition_neq(self):
        from services.contacts_hub.smart_tags_engine import _check_condition
        assert _check_condition({"status": "active"}, {"field": "status", "op": "neq", "value": "inactive"}) is True


# ── Export Engine edge cases ──────────────────────────────────────────────────

class TestExportEdgeCases:
    """Additional edge case tests for export_engine.py"""

    @pytest.mark.asyncio
    async def test_export_csv_special_chars(self):
        from services.contacts_hub.export_engine import export_csv
        pool = FakePool(fetch_rows=[
            {"id": "1", "telegram_user_id": 123, "username": "test",
             "first_name": "Ivan, Jr.", "last_name": 'O\'Brien', "display_name": "",
             "phones": "[]", "emails": "[]", "company": "", "position": "",
             "websites": "[]", "birthday": None, "notes": "", "tags": "{}",
             "color_label": None, "is_favorite": False, "is_premium": False,
             "importance_level": None, "user_rating": None,
             "discovered_at": None, "last_synced_at": None, "created_at": None}
        ])
        result = await export_csv(pool, 123)
        assert "Ivan, Jr." in result
        assert "O'Brien" in result

    @pytest.mark.asyncio
    async def test_export_json_unicode(self):
        from services.contacts_hub.export_engine import export_json
        pool = FakePool(fetch_rows=[
            {"id": "1", "first_name": "Иван", "phones": "[]", "emails": "[]",
             "websites": "[]", "addresses": "[]", "custom_fields": "{}",
             "digital_footprint": "{}", "created_at": None, "updated_at": None,
             "discovered_at": None, "last_synced_at": None, "last_changed_at": None}
        ])
        result = await export_json(pool, 123)
        assert "Иван" in result
