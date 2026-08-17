"""Контакты: авто-обогащение (активность + язык по письменности) + wiring."""
from __future__ import annotations

import os

from services.contacts_hub import enrich

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_activity_levels():
    assert enrich.activity_level("online")["level"] == "hot"
    assert enrich.activity_level("last_week")["level"] == "warm"
    assert enrich.activity_level("offline")["level"] == "cold"
    assert enrich.activity_level(None)["level"] == "unknown"


def test_premium_warms_cold_activity():
    assert enrich.activity_level("offline", is_premium=False)["level"] == "cold"
    assert enrich.activity_level("offline", is_premium=True)["level"] == "warm"


def test_guess_language_by_script():
    assert enrich.guess_language("Иван", "Петров")["code"] == "ru"
    assert enrich.guess_language("John", "Smith")["code"] == "en"
    assert enrich.guess_language("", "")["code"] is None


def test_latin_confidence_discounted():
    ru = enrich.guess_language("Иван", "")
    en = enrich.guess_language("John", "")
    # латиница — слабый сигнал, уверенность ниже чистой доли
    assert en["confidence"] < ru["confidence"]


def test_enrich_bundles_both():
    e = enrich.enrich({"last_seen_type": "online", "first_name": "Иван",
                       "is_premium": False})
    assert e["activity"]["level"] == "hot"
    assert e["language"]["code"] == "ru"


def test_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "enrich as _enr" in api
    assert '"enriched"' in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "d.enriched" in html
    assert "enr.activity" in html and "enr.language" in html
