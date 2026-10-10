"""Инвайтинг: A/B welcome — вступившие делятся по вариантам приветствия."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_chain_welcome_supports_ab_variants():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = src.index("async def _chain_welcome")
    window = src[i:i + 3600]
    assert "ab_engine.clean_variants(w.get(\"variants\"))" in window
    assert "ab_engine.split_audience" in window
    # каждый вариант помечается ab_batch/ab_variant — как рассылка (для сводки A/B)
    assert '"ab_variant"' in window and '"ab_batch"' in window


def test_backend_accepts_welcome_variants():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert 'clean_variants(body.get("welcome_variants"))' in api
    # A/B имеет приоритет над одиночным text, но одиночный остаётся как fallback
    i = api.index('clean_variants(body.get("welcome_variants"))')
    window = api[i - 300:i + 700]
    assert '"variants": _wvars' in window
    assert '"text": _wt' in window


def test_frontend_welcome_ab_ui():
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
    assert "miWelcomeAbOn" in html
    assert "function toggleWelcomeAb" in html
    assert "body.welcome_variants" in html
