"""Регрессия: Global Presence — референс-унификация + генерация целей по гео.

Mini-app путь Global Presence раньше НЕ генерировал цели (план ставился, но
build_targets не вызывался — исполнитель находил 0 целей). Плюс новая фича:
унификация названия/username из референса ('Новости Москва' → 'Новости {{CITY_NAME}}').
"""
from __future__ import annotations

from services.presence_planner import (
    derive_pattern_from_reference,
    render_pattern,
    build_targets,
)


# ── derive_pattern_from_reference ────────────────────────────────────────────

def test_derive_name_from_reference():
    assert derive_pattern_from_reference("Новости Москва", "Москва", "{{CITY_NAME}}") == "Новости {{CITY_NAME}}"


def test_derive_username_from_reference():
    assert derive_pattern_from_reference("novosti_moscow", "moscow", "{{CITY_SLUG}}") == "novosti_{{CITY_SLUG}}"


def test_derive_case_insensitive():
    assert derive_pattern_from_reference("MOSCOW news", "moscow", "{{CITY}}") == "{{CITY}} news"


def test_derive_empty_reference():
    assert derive_pattern_from_reference("", "Москва") == ""


def test_derive_city_not_in_reference_returns_reference():
    # город не встречается — возвращаем референс как есть
    assert derive_pattern_from_reference("Глобальный канал", "Москва", "{{CITY_NAME}}") == "Глобальный канал"


def test_derive_empty_sample_returns_reference():
    assert derive_pattern_from_reference("Новости", "", "{{CITY_NAME}}") == "Новости"


# ── render_pattern + build_targets end-to-end ────────────────────────────────

def test_render_pattern_placeholders():
    geo = {"city": "Moscow", "city_native": "Москва", "city_slug": "moscow",
           "country": "Russia", "country_code": "ru"}
    assert render_pattern("Новости {{CITY_NAME}}", geo) == "Новости Москва"
    assert render_pattern("news_{{CITY_SLUG}}", geo) == "news_moscow"
    assert render_pattern("{{COUNTRY_CODE}} {{CITY}}", geo) == "RU Moscow"


def test_build_targets_generates_per_city():
    geo_list = [
        {"city": "Moscow", "city_native": "Москва", "city_slug": "moscow"},
        {"city": "Kyiv", "city_native": "Київ", "city_slug": "kyiv"},
    ]
    targets = build_targets(geo_list, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}", [1, 2])
    assert len(targets) == 2
    assert targets[0]["planned_name"] == "Новости Москва"
    assert targets[0]["planned_username"] == "novosti_moscow"
    assert targets[1]["planned_name"] == "Новости Київ"
    assert targets[1]["planned_username"] == "novosti_kyiv"
    # аккаунты распределяются по кругу
    assert targets[0]["selected_account_id"] == 1
    assert targets[1]["selected_account_id"] == 2


def test_build_targets_username_slugified_and_capped():
    geo_list = [{"city": "X" * 50, "city_slug": "x" * 50}]
    targets = build_targets(geo_list, "channel", "chan", "u_{{CITY_SLUG}}", [1])
    # username ≤ 32 символа (ограничение Telegram)
    assert len(targets[0]["planned_username"]) <= 32
