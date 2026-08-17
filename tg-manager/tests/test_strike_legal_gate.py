"""Strike: категорийный гейт + подписанное правовое основание + подтверждение."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api():
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def test_launch_rejects_unknown_category():
    src = _api()
    i = src.index("async def strike_launch")
    window = src[i:i + 2600]
    # неизвестная категория отбивается (гейт по abuse-таксономии)
    assert "MINI_CATEGORIES.get(category)" in window
    assert "Неизвестная категория" in window


def test_launch_records_signed_legal_basis():
    src = _api()
    i = src.index("async def strike_launch")
    window = src[i:i + 5200]
    assert "compliance_engine.record" in window
    assert "strike_legal_basis" in window
    assert "abuse_report" in window


def test_frontend_has_legal_confirmation():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    i = html.index("async function strikeLaunch")
    window = html[i:i + 700]
    assert "askConfirm" in window
    assert "Правовое подтверждение" in window
