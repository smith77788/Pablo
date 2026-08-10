"""Tests for op_worker executors and remaining critical services.

Covers: op_worker dispatch logic, op types, operation_bus registry,
account_warmup, strike_engine, ecosystem_brain, parser, and more.
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


# ── Op Worker tests ───────────────────────────────────────────────────────────

class TestOpWorker:
    """Tests for services/op_worker.py"""

    def test_module_importable(self):
        import services.op_worker
        assert hasattr(services.op_worker, '__name__')

    def test_has_run_function(self):
        from services.op_worker import run
        assert callable(run)

    def test_has_exec_functions(self):
        from services import op_worker
        exec_funcs = [name for name in dir(op_worker) if name.startswith('_exec_')]
        assert len(exec_funcs) >= 40

    def test_exec_function_coverage(self):
        from services import op_worker
        exec_funcs = [name for name in dir(op_worker) if name.startswith('_exec_')]
        key_types = [
            '_exec_bulk_bot_edit', '_exec_dm_campaign', '_exec_mass_publish',
            '_exec_bulk_join', '_exec_bulk_leave', '_exec_strike',
            '_exec_mass_invite', '_exec_content_clone', '_exec_boost_views',
            '_exec_auto_register', '_exec_parse_audience', '_exec_run_broadcast',
        ]
        for kt in key_types:
            assert kt in exec_funcs, f"Missing executor: {kt}"


# ── Operation Bus tests ───────────────────────────────────────────────────────

class TestOperationBus:
    """Tests for services/operation_bus.py"""

    def test_module_importable(self):
        import services.operation_bus
        assert hasattr(services.operation_bus, '__name__')

    def test_op_registry_completeness(self):
        from services.operation_bus import OP_REGISTRY
        assert len(OP_REGISTRY) >= 30, f"OP_REGISTRY has only {len(OP_REGISTRY)} entries, expected >= 30"
        critical_ops = [
            'mass_publish', 'bulk_join', 'bulk_leave', 'strike',
            'dm_campaign', 'mass_invite', 'content_clone', 'bulk_bot_edit',
            'global_presence_channel', 'global_presence_bot',
            'bulk_create_channels', 'bot_factory', 'network_broadcast',
            'seed_presence_pack', 'promote_presence_pack',
            'bulk_edit_channels', 'group_import_all', 'group_announce',
            'bulk_dm_adhoc', 'bulk_post_to_channel', 'pin_last_post',
            'bulk_update_profile', 'bulk_chan_exec', 'bulk_post_chans',
            'channel_import_all', 'check_accounts_health',
            'scan_owned_resources', 'promote_all_admins',
            'boost_views', 'boost_reactions', 'boost_stories',
            'boost_subscribers', 'boost_bot_starts',
            'bulk_set_profile', 'mass_report', 'niche_growth_post',
            'parse_audience', 'reg_check',
            'ad_intel_scan', 'self_promo_blast', 'phone_check',
            'gift_scan', 'report_peer', 'auto_register',
            'leave_all_chats', 'delete_contacts', 'run_broadcast',
            'clone_adapt', 'quick_post', 'create_channel', 'create_group',
        ]
        present = sum(1 for op in critical_ops if op in OP_REGISTRY)
        assert present >= 30, f"Only {present}/{len(critical_ops)} critical ops in registry"


# ── Account Warmer tests ──────────────────────────────────────────────────────

class TestAccountWarmer:
    """Tests for services/account_warmer.py"""

    def test_module_importable(self):
        import services.account_warmer
        assert hasattr(services.account_warmer, '__name__')


# ── Strike Engine tests ───────────────────────────────────────────────────────

class TestStrikeEngine:
    """Tests for services/strike_engine.py"""

    def test_module_importable(self):
        import services.strike_engine
        assert hasattr(strike_engine := services.strike_engine, '__name__')


# ── Ecosystem Brain tests ─────────────────────────────────────────────────────

class TestEcosystemBrain:
    """Tests for services/ecosystem_brain.py"""

    def test_module_importable(self):
        import services.ecosystem_brain
        assert hasattr(services.ecosystem_brain, '__name__')


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestParser:
    """Tests for services/parser.py"""

    def test_module_importable(self):
        import services.parser
        assert hasattr(services.parser, '__name__')


# ── Activity Engine tests ─────────────────────────────────────────────────────

class TestActivityEngine:
    """Tests for services/activity_engine.py"""

    def test_module_importable(self):
        import services.activity_engine
        assert hasattr(services.activity_engine, '__name__')


# ── Auto Responder tests ──────────────────────────────────────────────────────

class TestAutoResponder:
    """Tests for services/auto_responder.py"""

    def test_module_importable(self):
        import services.auto_responder
        assert hasattr(services.auto_responder, '__name__')


# ── Broadcaster tests ─────────────────────────────────────────────────────────

class TestBroadcaster:
    """Tests for services/broadcaster.py"""

    def test_module_importable(self):
        import services.broadcaster
        assert hasattr(services.broadcaster, '__name__')


# ── Infra Copilot tests ───────────────────────────────────────────────────────

class TestInfraCopilot:
    """Tests for services/infra_copilot.py"""

    def test_module_importable(self):
        import services.infra_copilot
        assert hasattr(services.infra_copilot, '__name__')


# ── Infra Memory tests ────────────────────────────────────────────────────────

class TestInfraMemory:
    """Tests for services/infra_memory.py"""

    def test_module_importable(self):
        import services.infra_memory
        assert hasattr(services.infra_memory, '__name__')


# ── Intelligence Engine tests ─────────────────────────────────────────────────

class TestIntelligenceEngine:
    """Tests for services/intelligence_engine.py"""

    def test_module_importable(self):
        import services.intelligence_engine
        assert hasattr(services.intelligence_engine, '__name__')


# ── Narrative Engine tests ────────────────────────────────────────────────────

class TestNarrativeEngine:
    """Tests for services/narrative_engine.py"""

    def test_module_importable(self):
        import services.narrative_engine
        assert hasattr(services.narrative_engine, '__name__')


# ── Session Pool tests ────────────────────────────────────────────────────────

class TestSessionPool:
    """Tests for services/session_pool.py"""

    def test_module_importable(self):
        import services.session_pool
        assert hasattr(services.session_pool, '__name__')


# ── Token Vault tests ─────────────────────────────────────────────────────────

class TestTokenVault:
    """Tests for services/token_vault.py"""

    def test_module_importable(self):
        import services.token_vault
        assert hasattr(services.token_vault, '__name__')


# ── Username Engine tests ─────────────────────────────────────────────────────

class TestUsernameEngine:
    """Tests for services/username_engine.py"""

    def test_module_importable(self):
        import services.username_engine
        assert hasattr(services.username_engine, '__name__')


# ── Funnel Runner tests ───────────────────────────────────────────────────────

class TestFunnelRunner:
    """Tests for services/funnel_runner.py"""

    def test_module_importable(self):
        import services.funnel_runner
        assert hasattr(services.funnel_runner, '__name__')


# ── Auto Funnel tests ─────────────────────────────────────────────────────────

class TestAutoFunnel:
    """Tests for services/auto_funnel.py"""

    def test_module_importable(self):
        import services.auto_funnel
        assert hasattr(services.auto_funnel, '__name__')


# ── Gift Operation tests ──────────────────────────────────────────────────────

class TestGiftOperation:
    """Tests for services/gift_operation.py"""

    def test_module_importable(self):
        import services.gift_operation
        assert hasattr(services.gift_operation, '__name__')


# ── Content Mesh tests ────────────────────────────────────────────────────────

class TestContentMesh:
    """Tests for services/content_mesh.py"""

    def test_module_importable(self):
        import services.content_mesh
        assert hasattr(services.content_mesh, '__name__')


# ── CF Relay tests ────────────────────────────────────────────────────────────

class TestCFRelay:
    """Tests for services/cf_relay.py"""

    def test_module_importable(self):
        import services.cf_relay
        assert hasattr(services.cf_relay, '__name__')


# ── SMS API Engine tests ──────────────────────────────────────────────────────

class TestSMSAPIEngine:
    """Tests for services/sms_api_engine.py"""

    def test_module_importable(self):
        import services.sms_api_engine
        assert hasattr(services.sms_api_engine, '__name__')


# ── Session Importer tests ────────────────────────────────────────────────────

class TestSessionImporter:
    """Tests for services/session_importer.py"""

    def test_module_importable(self):
        import services.session_importer
        assert hasattr(services.session_importer, '__name__')


# ── TData Converter tests ─────────────────────────────────────────────────────

class TestTDataConverter:
    """Tests for services/tdata_converter.py"""

    def test_module_importable(self):
        import services.tdata_converter
        assert hasattr(services.tdata_converter, '__name__')


# ── Ranking Checker tests ─────────────────────────────────────────────────────

class TestRankingChecker:
    """Tests for services/ranking_checker.py"""

    def test_module_importable(self):
        import services.ranking_checker
        assert hasattr(services.ranking_checker, '__name__')


# ── Spintax AI tests ──────────────────────────────────────────────────────────

class TestSpintaxAI:
    """Tests for services/spintax_ai.py"""

    def test_module_importable(self):
        import services.spintax_ai
        assert hasattr(services.spintax_ai, '__name__')


# ── Botmother Channel tests ───────────────────────────────────────────────────

class TestBotmotherChannel:
    """Tests for services/botmother_channel.py"""

    def test_module_importable(self):
        import services.botmother_channel
        assert hasattr(services.botmother_channel, '__name__')


class TestNormalizeResultFailedAlias:
    """_normalize_result: канонизация счётчика провалов "fail" → "failed".

    Регрессия реального бага: ~25 exec-функций отдают провалы под ключом "fail",
    а нормализатор статуса в _run_op_task читает "failed". Полностью провальная
    операция (ok=0, ВСЕ цели упали) приходила как ok=0/failed=0 и помечалась
    "done" (успех) — пользователь видел успех у пустой рассылки, circuit breaker
    и pacing получали ложный сигнал успеха. Нормализация алиаса чинит всех сразу.
    """

    def test_fail_alias_becomes_failed(self):
        from services.op_worker import _normalize_result
        r = _normalize_result({"status": "done", "ok": 0, "fail": 3}, "self_promo_blast", 1.0)
        assert r["failed"] == 3
        assert r["total"] == 3  # ok(0)+failed(3), а не ok(0)+0

    def test_canonical_failed_key_is_respected(self):
        from services.op_worker import _normalize_result
        r = _normalize_result({"status": "done", "ok": 5, "failed": 2}, "bulk_join", 1.0)
        assert r["failed"] == 2

    def test_no_failure_stays_zero(self):
        from services.op_worker import _normalize_result
        r = _normalize_result({"status": "done", "ok": 4}, "mass_publish", 1.0)
        assert r["failed"] == 0

    def test_failed_takes_precedence_over_fail_when_both_present(self):
        from services.op_worker import _normalize_result
        # "failed" каноничен: если он уже есть, "fail" не перетирает его.
        r = _normalize_result({"status": "done", "ok": 1, "failed": 7, "fail": 999}, "x", 1.0)
        assert r["failed"] == 7
