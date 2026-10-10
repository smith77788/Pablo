"""Регрессия: фильтры выборки спарсенной аудитории.

Богатые колонки (is_premium/is_bot/is_active/phone/username) хранились в
parsed_audiences, но не фильтровались (get_parsed_audience умел только active_only).
parsed_audience_filters строит SQL-условия под просмотр и CSV-экспорт.
"""
from __future__ import annotations

from services.mini_app_api import parsed_audience_filters


def test_no_filters_empty():
    sql, params = parsed_audience_filters({})
    assert sql == ""
    assert params == []


def test_source_adds_param_placeholder():
    sql, params = parsed_audience_filters({"source": "crypto"}, base_params_count=1)
    assert "source_username ILIKE $2" in sql
    assert params == ["%crypto%"]


def test_boolean_filters_are_literal_no_params():
    sql, params = parsed_audience_filters(
        {"premium": "1", "with_username": "1", "not_bot": "1", "active": "1", "with_phone": "1"}
    )
    assert "is_premium=TRUE" in sql
    assert "username IS NOT NULL" in sql
    assert "COALESCE(is_bot,FALSE)=FALSE" in sql
    assert "is_active=TRUE" in sql
    assert "phone IS NOT NULL" in sql
    assert params == []  # булевы — литералы, не параметры


def test_source_and_booleans_combined_placeholder_index():
    sql, params = parsed_audience_filters({"source": "x", "premium": "1"}, base_params_count=1)
    # source занимает $2, булев premium — литерал
    assert "$2" in sql and "is_premium=TRUE" in sql
    assert params == ["%x%"]


def test_falsey_values_ignored():
    sql, params = parsed_audience_filters({"premium": "0", "active": "false", "not_bot": "no"})
    assert sql == ""
    assert params == []


def test_starts_with_and_prefix():
    # ведущий ' AND ' обязателен, чтобы подклеиться к WHERE owner_id=$1
    sql, _ = parsed_audience_filters({"active": "1"})
    assert sql.startswith(" AND ")


def test_gender_filter_adds_param():
    sql, params = parsed_audience_filters({"gender": "f"}, base_params_count=1)
    assert "gender=$2" in sql
    assert params == ["f"]


def test_gender_invalid_ignored():
    for bad in ("", "x", "male", None):
        sql, params = parsed_audience_filters({"gender": bad})
        assert "gender=" not in sql
        assert params == []


def test_gender_placeholder_index_with_source_and_last_seen():
    # порядок условий: source → $2, gender → $3, last_seen → $4 (индексы монотонны)
    sql, params = parsed_audience_filters(
        {"source": "x", "last_seen": "7", "gender": "m"}, base_params_count=1)
    assert "source_username ILIKE $2" in sql
    assert "gender=$3" in sql
    assert "last_seen_days IS NOT NULL AND last_seen_days <= $4" in sql
    assert params == ["%x%", "m", 7]
