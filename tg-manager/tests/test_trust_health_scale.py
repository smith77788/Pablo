"""Регресс: trust_score хранится в 0..1, а UI показывает проценты 0..100.

Баг «уровень здоровья у всех аккаунтов одинаковый ~1%»: display-эндпоинты
отдавали сырой trust_score (0..1) как процент и/или сравнивали 0..1-значение с
порогами 40/70 (как будто 0..100) — все аккаунты падали в «bad» с avg≈1.
Фикс: везде на UI-путях ROUND(COALESCE(trust_score,1.0)*100) и пороги на 0..1.
"""
from __future__ import annotations

import os
from tests.miniapp_source import api_source, miniapp_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api() -> str:
    return api_source()


def test_display_endpoints_scale_trust_to_percent():
    src = _api()
    # список/деталь/экспорт аккаунтов отдают trust как проценты 0..100
    assert src.count("ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score") >= 4
    # старый баг: сырой 0..1 отдавался как процент (дефолт 100 при 0..1-данных)
    assert "COALESCE(trust_score, 100) AS trust_score" not in src


def test_accounts_health_aggregate_uses_correct_scale():
    src = _api()
    # Здоровье командного центра теперь health-aware (durable-сигналы единого
    # пульса), а не чистый trust: trust масштабируется *100 ВНУТРИ выражения,
    # к нему добавлены мёртвый статус/нет сессии/кулдаун/флуд; пороги good/warn/
    # bad на 0..100.
    assert "ROUND(AVG(h))::int AS avg" in src
    assert "LEAST(100, COALESCE(trust_score,1.0) * 100)" in src
    assert "flood_count_7d" in src and "cooldown_until" in src and "acc_status" in src
    assert "h >= 70" in src and "h < 40" in src
    # старый баг 0..1 как проценты не вернулся
    assert "AVG(COALESCE(trust_score,50))" not in src
    assert "COALESCE(trust_score,50) >= 70" not in src


def test_frontend_zero_trust_not_treated_as_100():
    html = miniapp_source()
    # trust=0 (нулевой траст) не должен становиться 100 из-за `||100` (0 — falsy)
    assert "Math.round(a.trust_score||100)" not in html
    assert html.count("a.trust_score!=null?Math.round(a.trust_score):100") >= 2
