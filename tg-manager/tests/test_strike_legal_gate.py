"""Strike: категорийный гейт + подписанное правовое основание + подтверждение."""
from __future__ import annotations

import os
from tests.miniapp_source import api_function, api_source, js_function

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api():
    return api_source()


def test_launch_rejects_unknown_category():
    window = api_function("strike_launch")
    # неизвестная категория отбивается (гейт по abuse-таксономии)
    assert "MINI_CATEGORIES.get(category)" in window
    assert "Неизвестная категория" in window


def test_launch_records_signed_legal_basis():
    window = api_function("strike_launch")
    assert "compliance_engine.record" in window
    assert "strike_legal_basis" in window
    assert "abuse_report" in window


def test_frontend_has_legal_confirmation():
    window = js_function("strikeLaunch")
    assert "askConfirm" in window
    assert "Правовое подтверждение" in window
