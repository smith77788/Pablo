"""Регресс: trust_score хранится в 0..1, а UI показывает проценты 0..100.

Баг «уровень здоровья у всех аккаунтов одинаковый ~1%»: display-эндпоинты
отдавали сырой trust_score (0..1) как процент и/или сравнивали 0..1-значение с
порогами 40/70 (как будто 0..100) — все аккаунты падали в «bad» с avg≈1.
Фикс: везде на UI-путях ROUND(COALESCE(trust_score,1.0)*100) и пороги на 0..1.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api() -> str:
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def test_display_endpoints_scale_trust_to_percent():
    src = _api()
    # список/деталь/экспорт аккаунтов отдают trust как проценты 0..100
    assert src.count("ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score") >= 4
    # старый баг: сырой 0..1 отдавался как процент (дефолт 100 при 0..1-данных)
    assert "COALESCE(trust_score, 100) AS trust_score" not in src


def test_accounts_health_aggregate_uses_correct_scale():
    src = _api()
    # avg здоровья = среднее 0..1 * 100; пороги хороший/риск/плохой — на 0..1
    assert "ROUND(AVG(COALESCE(trust_score,1.0)) * 100)::int AS avg" in src
    assert "COALESCE(trust_score,1.0) >= 0.70" in src
    assert "COALESCE(trust_score,1.0) < 0.40" in src
    # старые пороги 0..100 на 0..1-данных (всё уходило в «bad»)
    assert "AVG(COALESCE(trust_score,50))" not in src
    assert "COALESCE(trust_score,50) >= 70" not in src


def test_frontend_zero_trust_not_treated_as_100():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    # trust=0 (нулевой траст) не должен становиться 100 из-за `||100` (0 — falsy)
    assert "Math.round(a.trust_score||100)" not in html
    assert html.count("a.trust_score!=null?Math.round(a.trust_score):100") >= 2
