"""SEO-советник: чистые эвристики находимости + наличие эндпоинта/экрана."""
from __future__ import annotations

import os

from services import seo_advisor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_perfect_object_scores_green():
    r = seo_advisor.analyze(
        title="Крипта Сигналы",
        username="crypto_signals",
        description="Ежедневные сигналы по криптовалютам, аналитика рынка и разбор сделок для трейдеров.",
        target_keywords=["крипта", "сигналы"])
    assert r["grade"] == "green"
    assert r["score"] >= 75
    assert all(r["keyword_hit"].values())


def test_empty_object_scores_red():
    r = seo_advisor.analyze()
    assert r["grade"] == "red"
    assert r["score"] < 50
    assert any("username" in i.lower() for i in r["issues"])
    assert any("заголов" in i.lower() for i in r["issues"])


def test_missing_keywords_penalized_and_reported():
    r = seo_advisor.analyze(
        title="Мой канал", username="mychan01",
        description="Просто какой-то текст без нужных слов вообще совсем.",
        target_keywords=["недвижимость", "ипотека"])
    assert r["keyword_hit"] == {"недвижимость": False, "ипотека": False}
    assert any("Ключи не в тексте" in i for i in r["issues"])


def test_caps_and_emoji_stuffing_flagged():
    r = seo_advisor.analyze(title="КАНАЛ КРИПТЫ 🚀🚀🚀🔥", username="ok_channel")
    joined = " ".join(r["issues"]).lower()
    assert "капс" in joined or "эмодзи" in joined


def test_score_bounded_and_grade_consistent():
    for args in [("", "", "", None), ("a" * 60, "x" * 40, "d" * 5, ["z"])]:
        r = seo_advisor.analyze(*args)
        assert 0 <= r["score"] <= 100
        assert r["grade"] in ("green", "amber", "red")


def test_endpoint_and_route_wired():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def seo_analyze" in src
    assert '"/api/miniapp/seo/analyze"' in src
    assert "seo_advisor.analyze" in src


def test_frontend_screen_and_handler_present():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-seoobj"' in html
    assert "function runSeoAnalyze" in html
    assert "function openSeoObj" in html
    assert "/api/miniapp/seo/analyze" in html
    # экран должен иметь уникальный id (не коллизия со старым s-seo)
    assert html.count('id="s-seoobj"') == 1
