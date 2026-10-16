"""Комплаенс: подписанный отчёт в обзоре + текстовая выгрузка (эндпоинт+UI)."""
from __future__ import annotations

import os
from tests.miniapp_source import api_function, api_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api():
    return api_source()


def _html():
    return open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def test_overview_includes_signed_report():
    window = api_function("compliance_overview")
    assert "compliance_engine.get_report" in window
    assert '"report"' in window


def test_export_endpoint_and_route():
    src = _api()
    assert "async def compliance_export" in src
    assert "compliance_engine.export_text" in src
    assert '"/api/miniapp/compliance/export"' in src
    # период клампится в разумные границы
    i = src.index("async def compliance_export")
    assert "min(days, 365)" in src[i:i + 800]


def test_frontend_export_button_and_handler():
    html = _html()
    assert "exportCompliance()" in html
    assert "function exportCompliance" in html
    assert "/api/miniapp/compliance/export" in html
    # обзор рисует успешность/охват из отчёта
    assert "success_rate" in html
    assert "distinct_types" in html
