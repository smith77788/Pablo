"""Tests for operation bus, DB helpers, and remaining services.

Covers: operation_bus, db helpers, account_health, proxy_selector.
"""
from __future__ import annotations

import json
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


# ── Operation Bus tests ───────────────────────────────────────────────────────

class TestOperationBus:
    """Tests for services/operation_bus.py"""

    def test_module_importable(self):
        import services.operation_bus
        assert hasattr(services.operation_bus, '__name__')

    def test_op_registry_has_types(self):
        from services.operation_bus import OP_REGISTRY
        assert isinstance(OP_REGISTRY, dict)
        assert len(OP_REGISTRY) > 0

    def test_op_registry_contains_key_types(self):
        from services.operation_bus import OP_REGISTRY
        key_types = ['mass_publish', 'bulk_join', 'bulk_leave', 'strike',
                     'dm_campaign', 'mass_invite', 'content_clone']
        for t in key_types:
            assert t in OP_REGISTRY, f"Missing op_type: {t}"


# ── DB Helper tests ───────────────────────────────────────────────────────────

class TestDBHelpers:
    """Tests for database/db.py helper functions"""

    def test_module_importable(self):
        import database.db
        assert hasattr(database.db, '__name__')

    def test_db_has_create_pool(self):
        from database.db import create_pool
        assert callable(create_pool)


# ── Account Health tests ──────────────────────────────────────────────────────

class TestAccountHealth:
    """Tests for services/account_health.py"""

    def test_module_importable(self):
        import services.account_health
        assert hasattr(services.account_health, '__name__')


# ── Proxy Selector tests ──────────────────────────────────────────────────────

class TestProxySelector:
    """Tests for services/proxy_selector.py"""

    def test_module_importable(self):
        import services.proxy_selector
        assert hasattr(services.proxy_selector, '__name__')


# ── Mini App API route count ──────────────────────────────────────────────────

class TestMiniAppRoutes:
    """Tests for mini_app_api.py route registration"""

    def test_api_has_routes(self):
        import services.mini_app_api
        assert hasattr(services.mini_app_api, 'setup_routes')

    def test_api_helpers_exist(self):
        from services.mini_app_api import _json_resp, _err, _get_uid, _is_admin
        assert callable(_json_resp)
        assert callable(_err)
        assert callable(_get_uid)
        assert callable(_is_admin)

    def test_is_safe_public_url_comprehensive(self):
        from services.mini_app_api import is_safe_public_url
        # Valid HTTPS URLs
        assert is_safe_public_url("https://example.com/img.jpg") is True
        assert is_safe_public_url("https://cdn.telegram.org/photo.png") is True
        # Invalid schemes
        assert is_safe_public_url("http://example.com/img.jpg") is False
        assert is_safe_public_url("ftp://example.com/img.jpg") is False
        # Localhost/internal
        assert is_safe_public_url("https://localhost/img.jpg") is False
        assert is_safe_public_url("https://127.0.0.1/img.jpg") is False
        assert is_safe_public_url("https://myapp.local/img.jpg") is False
        assert is_safe_public_url("https://server.internal/img.jpg") is False
        # Edge cases
        assert is_safe_public_url("") is False
        assert is_safe_public_url(None) is False
        assert is_safe_public_url("not a url") is False


# ── Scheduler tests ───────────────────────────────────────────────────────────

class TestScheduler:
    """Tests for services/scheduler.py"""

    def test_module_importable(self):
        import services.scheduler
        assert hasattr(services.scheduler, '__name__')


# ── Content Cloner Engine tests ───────────────────────────────────────────────

class TestContentClonerEngine:
    """Tests for services/content_cloner_engine.py"""

    def test_module_importable(self):
        import services.content_cloner_engine
        assert hasattr(services.content_cloner_engine, '__name__')


# ── Profile Setter Engine tests ───────────────────────────────────────────────

class TestProfileSetterEngine:
    """Tests for services/profile_setter_engine.py"""

    def test_module_importable(self):
        import services.profile_setter_engine
        assert hasattr(services.profile_setter_engine, '__name__')


# ── Reporter Engine tests ─────────────────────────────────────────────────────

class TestReporterEngine:
    """Tests for services/reporter_engine.py"""

    def test_module_importable(self):
        import services.reporter_engine
        assert hasattr(services.reporter_engine, '__name__')


# ── Boost Engine tests ────────────────────────────────────────────────────────

class TestBoostEngine:
    """Tests for services/boost_engine.py"""

    def test_module_importable(self):
        import services.boost_engine
        assert hasattr(services.boost_engine, '__name__')


# ── DM Engine tests ───────────────────────────────────────────────────────────

class TestDMEngine:
    """Tests for services/dm_engine.py"""

    def test_module_importable(self):
        import services.dm_engine
        assert hasattr(services.dm_engine, '__name__')
