"""Tests for KPIAnalyzer — covers all 8 KPIs from БЛОК 5.7."""
from __future__ import annotations
import datetime
import sqlite3
import os
import pytest

from factory.agents.kpi_analyzer import KPIAnalyzer, _connect_db, DB_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the Nevesty schema and seed data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            model_id INTEGER,
            client_chat_id TEXT,
            status TEXT DEFAULT 'new',
            event_type TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(model_id) REFERENCES models(id)
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY,
            model_id INTEGER,
            rating REAL,
            approved INTEGER DEFAULT 0,
            FOREIGN KEY(model_id) REFERENCES models(id)
        );

        INSERT INTO models (id, name, category) VALUES
            (1, 'Anna',   'Fashion'),
            (2, 'Bella',  'Commercial'),
            (3, 'Clara',  'Fashion'),
            (4, 'Diana',  NULL);

        INSERT INTO orders (id, model_id, client_chat_id, status, event_type, created_at, updated_at) VALUES
            (1,  1, 'c1', 'completed',  'wedding',    date('now', '-40 days'), date('now', '-35 days')),
            (2,  2, 'c2', 'confirmed',  'corporate',  date('now', '-20 days'), date('now', '-18 days')),
            (3,  1, 'c3', 'new',        'wedding',    date('now', '-5 days'),  NULL),
            (4,  3, 'c1', 'completed',  'party',      date('now', '-3 days'),  date('now', '-1 days')),
            (5,  2, 'c4', 'cancelled',  'corporate',  date('now', '-2 days'),  date('now', '-1 days')),
            (6,  1, 'c2', 'new',        'wedding',    date('now'),             NULL),
            (7,  3, 'c5', 'completed',  'party',      date('now', '-60 days'), date('now', '-58 days')),
            (8,  4, 'c6', 'confirmed',  NULL,         date('now', '-10 days'), date('now', '-9 days'));

        INSERT INTO reviews (id, model_id, rating, approved) VALUES
            (1, 1, 5.0, 1),
            (2, 1, 4.0, 1),
            (3, 2, 3.0, 1),
            (4, 3, 5.0, 0),
            (5, 2, 4.5, 1),
            (6, 1, 5.0, 0);
    """)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# _connect_db
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectDb:
    def test_returns_none_for_nonexistent_path(self):
        result = _connect_db("/nonexistent/path/to/db.sqlite")
        assert result is None

    def test_returns_none_for_empty_path(self, tmp_path):
        result = _connect_db(str(tmp_path / "missing.db"))
        assert result is None

    def test_returns_connection_for_valid_db(self, tmp_path):
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_file))
        conn.close()
        result = _connect_db(str(db_file))
        assert result is not None
        result.close()

    def test_connection_has_row_factory(self, tmp_path):
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()
        result = _connect_db(str(db_file))
        assert result is not None
        assert result.row_factory == sqlite3.Row
        result.close()


# ─────────────────────────────────────────────────────────────────────────────
# KPIAnalyzer instantiation
# ─────────────────────────────────────────────────────────────────────────────

class TestKPIAnalyzerInit:
    def test_default_instantiation(self):
        analyzer = KPIAnalyzer()
        assert analyzer is not None

    def test_default_db_path(self):
        analyzer = KPIAnalyzer()
        assert analyzer.db_path == DB_PATH

    def test_custom_db_path(self):
        analyzer = KPIAnalyzer(db_path="/custom/path.db")
        assert analyzer.db_path == "/custom/path.db"

    def test_instance_has_all_methods(self):
        analyzer = KPIAnalyzer()
        assert callable(analyzer.analyze_orders_per_period)
        assert callable(analyzer.analyze_conversion_by_source)
        assert callable(analyzer.analyze_popular_categories)
        assert callable(analyzer.analyze_client_return_rate)
        assert callable(analyzer.analyze_deal_cycle_days)
        assert callable(analyzer.analyze_model_ratings)
        assert callable(analyzer.analyze_bot_activity)
        assert callable(analyzer.analyze_top_client_requests)
        assert callable(analyzer.run_full_analysis)
        assert callable(analyzer.generate_summary)


# ─────────────────────────────────────────────────────────────────────────────
# KPI 1: analyze_orders_per_period
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeOrdersPerPeriod:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_dict_when_conn_is_none(self):
        result = self.analyzer.analyze_orders_per_period(None)
        assert isinstance(result, dict)

    def test_contains_required_keys_when_none(self):
        result = self.analyzer.analyze_orders_per_period(None)
        assert "today" in result
        assert "week" in result
        assert "month" in result
        assert "total" in result

    def test_all_zeros_when_conn_is_none(self):
        result = self.analyzer.analyze_orders_per_period(None)
        assert result["today"] == 0
        assert result["week"] == 0
        assert result["month"] == 0
        assert result["total"] == 0

    def test_returns_correct_total_with_real_db(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        assert result["total"] == 8

    def test_today_count_correct(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        # Order id=6 was created today
        assert result["today"] >= 1

    def test_week_count_gte_today(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        assert result["week"] >= result["today"]

    def test_month_count_gte_week(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        assert result["month"] >= result["week"]

    def test_total_gte_month(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        assert result["total"] >= result["month"]

    def test_returns_integers(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        for key in ("today", "week", "month", "total"):
            assert isinstance(result[key], int)


# ─────────────────────────────────────────────────────────────────────────────
# KPI 2: analyze_conversion_by_source
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeConversionBySource:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_dict_when_none(self):
        result = self.analyzer.analyze_conversion_by_source(None)
        assert isinstance(result, dict)

    def test_required_keys_when_none(self):
        result = self.analyzer.analyze_conversion_by_source(None)
        assert "total" in result
        assert "completed" in result
        assert "conversion_rate" in result

    def test_zeros_when_none(self):
        result = self.analyzer.analyze_conversion_by_source(None)
        assert result["total"] == 0
        assert result["completed"] == 0
        assert result["conversion_rate"] == 0.0

    def test_conversion_rate_is_float_when_none(self):
        result = self.analyzer.analyze_conversion_by_source(None)
        assert isinstance(result["conversion_rate"], float)

    def test_total_matches_order_count(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_conversion_by_source(conn)
        conn.close()
        assert result["total"] == 8

    def test_completed_counts_confirmed_and_completed(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_conversion_by_source(conn)
        conn.close()
        # confirmed: id=2,8 → 2, completed: id=1,4,7 → 3 → total 5
        assert result["completed"] == 5

    def test_conversion_rate_between_0_and_1(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_conversion_by_source(conn)
        conn.close()
        assert 0.0 <= result["conversion_rate"] <= 1.0

    def test_conversion_rate_formula(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_conversion_by_source(conn)
        conn.close()
        expected = round(result["completed"] / result["total"], 3)
        assert result["conversion_rate"] == expected


# ─────────────────────────────────────────────────────────────────────────────
# KPI 3: analyze_popular_categories
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzePopularCategories:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_list_when_none(self):
        result = self.analyzer.analyze_popular_categories(None)
        assert isinstance(result, list)

    def test_empty_list_when_none(self):
        result = self.analyzer.analyze_popular_categories(None)
        assert result == []

    def test_returns_list_with_real_db(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_popular_categories(conn)
        conn.close()
        assert isinstance(result, list)

    def test_each_item_has_category_and_count(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_popular_categories(conn)
        conn.close()
        for item in result:
            assert "category" in item
            assert "order_count" in item

    def test_sorted_by_order_count_desc(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_popular_categories(conn)
        conn.close()
        counts = [item["order_count"] for item in result]
        assert counts == sorted(counts, reverse=True)

    def test_null_category_models_excluded(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_popular_categories(conn)
        conn.close()
        categories = [item["category"] for item in result]
        assert None not in categories

    def test_known_categories_present(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_popular_categories(conn)
        conn.close()
        categories = {item["category"] for item in result}
        # Fashion (models 1, 3) and Commercial (model 2) have orders
        assert "Fashion" in categories
        assert "Commercial" in categories


# ─────────────────────────────────────────────────────────────────────────────
# KPI 4: analyze_client_return_rate
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeClientReturnRate:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_dict_when_none(self):
        result = self.analyzer.analyze_client_return_rate(None)
        assert isinstance(result, dict)

    def test_required_keys_when_none(self):
        result = self.analyzer.analyze_client_return_rate(None)
        assert "total_clients" in result
        assert "returning_clients" in result
        assert "return_rate" in result

    def test_zeros_when_none(self):
        result = self.analyzer.analyze_client_return_rate(None)
        assert result["total_clients"] == 0
        assert result["returning_clients"] == 0
        assert result["return_rate"] == 0.0

    def test_total_clients_correct(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_client_return_rate(conn)
        conn.close()
        # Unique clients: c1, c2, c3, c4, c5, c6 → 6
        assert result["total_clients"] == 6

    def test_returning_clients_correct(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_client_return_rate(conn)
        conn.close()
        # c1 has orders 1 and 4 (2 orders), c2 has orders 2 and 6 (2 orders) → 2 returning
        assert result["returning_clients"] == 2

    def test_return_rate_between_0_and_1(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_client_return_rate(conn)
        conn.close()
        assert 0.0 <= result["return_rate"] <= 1.0

    def test_return_rate_formula(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_client_return_rate(conn)
        conn.close()
        expected = round(result["returning_clients"] / result["total_clients"], 3)
        assert result["return_rate"] == expected


# ─────────────────────────────────────────────────────────────────────────────
# KPI 5: analyze_deal_cycle_days
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeDealCycleDays:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_dict_when_none(self):
        result = self.analyzer.analyze_deal_cycle_days(None)
        assert isinstance(result, dict)

    def test_required_keys_when_none(self):
        result = self.analyzer.analyze_deal_cycle_days(None)
        assert "avg_days" in result
        assert "min_days" in result
        assert "max_days" in result
        assert "sample_size" in result

    def test_zeros_when_none(self):
        result = self.analyzer.analyze_deal_cycle_days(None)
        assert result["avg_days"] == 0
        assert result["min_days"] == 0
        assert result["max_days"] == 0
        assert result["sample_size"] == 0

    def test_sample_size_matches_completed_orders(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_deal_cycle_days(conn)
        conn.close()
        # Completed orders with updated_at: ids 1, 4, 7 → 3
        assert result["sample_size"] == 3

    def test_avg_days_is_non_negative(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_deal_cycle_days(conn)
        conn.close()
        assert result["avg_days"] >= 0

    def test_min_lte_avg_lte_max(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_deal_cycle_days(conn)
        conn.close()
        if result["sample_size"] > 0:
            assert result["min_days"] <= result["avg_days"] <= result["max_days"]

    def test_empty_db_returns_zeros(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders (id INTEGER, status TEXT, created_at TEXT, updated_at TEXT)")
        result = self.analyzer.analyze_deal_cycle_days(conn)
        conn.close()
        assert result["sample_size"] == 0
        assert result["avg_days"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# KPI 6: analyze_model_ratings
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeModelRatings:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_list_when_none(self):
        result = self.analyzer.analyze_model_ratings(None)
        assert isinstance(result, list)

    def test_empty_when_none(self):
        result = self.analyzer.analyze_model_ratings(None)
        assert result == []

    def test_returns_list_with_real_db(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        assert isinstance(result, list)

    def test_only_approved_reviews_counted(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        # Anna: reviews 1(5.0,✓),2(4.0,✓) → avg 4.5 (review 6 not approved)
        # Commercial(2): reviews 3(3.0,✓),5(4.5,✓) → avg 3.75
        # Clara(3): review 4(5.0) NOT approved → should NOT appear
        model_names = {r["name"] for r in result}
        assert "Clara" not in model_names

    def test_each_item_has_required_keys(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        for item in result:
            assert "name" in item
            assert "model_id" in item
            assert "avg_rating" in item
            assert "review_count" in item

    def test_sorted_by_avg_rating_desc(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        ratings = [item["avg_rating"] for item in result]
        assert ratings == sorted(ratings, reverse=True)

    def test_anna_has_correct_avg_rating(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        anna = next((r for r in result if r["name"] == "Anna"), None)
        assert anna is not None
        # reviews: 5.0, 4.0 → avg 4.5
        assert anna["avg_rating"] == 4.5
        assert anna["review_count"] == 2

    def test_max_10_results(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_model_ratings(conn)
        conn.close()
        assert len(result) <= 10


# ─────────────────────────────────────────────────────────────────────────────
# KPI 7: analyze_bot_activity
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeBotActivity:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_dict_when_none(self):
        result = self.analyzer.analyze_bot_activity(None)
        assert isinstance(result, dict)

    def test_required_keys_when_none(self):
        result = self.analyzer.analyze_bot_activity(None)
        assert "unique_users" in result
        assert "active_sessions" in result
        assert "users_this_week" in result

    def test_zeros_when_none(self):
        result = self.analyzer.analyze_bot_activity(None)
        assert result["unique_users"] == 0
        assert result["active_sessions"] == 0
        assert result["users_this_week"] == 0

    def test_unique_users_correct(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_bot_activity(conn)
        conn.close()
        # c1, c2, c3, c4, c5, c6 → 6
        assert result["unique_users"] == 6

    def test_users_this_week_lte_unique_users(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_bot_activity(conn)
        conn.close()
        assert result["users_this_week"] <= result["unique_users"]

    def test_active_sessions_zero_without_table(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_bot_activity(conn)
        conn.close()
        # No telegram_sessions table in test schema
        assert result["active_sessions"] == 0

    def test_active_sessions_with_telegram_sessions_table(self):
        conn = _make_in_memory_db()
        conn.execute("""
            CREATE TABLE telegram_sessions (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                updated_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO telegram_sessions VALUES (1, 'u1', datetime('now', '-30 minutes'))"
        )
        conn.execute(
            "INSERT INTO telegram_sessions VALUES (2, 'u2', datetime('now', '-2 hours'))"
        )
        conn.commit()
        result = self.analyzer.analyze_bot_activity(conn)
        conn.close()
        assert result["active_sessions"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# KPI 8: analyze_top_client_requests
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeTopClientRequests:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_returns_list_when_none(self):
        result = self.analyzer.analyze_top_client_requests(None)
        assert isinstance(result, list)

    def test_empty_when_none(self):
        result = self.analyzer.analyze_top_client_requests(None)
        assert result == []

    def test_returns_list_with_real_db(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        assert isinstance(result, list)

    def test_each_item_has_event_type_and_count(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        for item in result:
            assert "event_type" in item
            assert "count" in item

    def test_sorted_by_count_desc(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        counts = [item["count"] for item in result]
        assert counts == sorted(counts, reverse=True)

    def test_max_5_results(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        assert len(result) <= 5

    def test_null_event_types_excluded(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        for item in result:
            assert item["event_type"] is not None

    def test_wedding_is_top_request(self):
        conn = _make_in_memory_db()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        # wedding appears 3 times (ids 1,3,6), corporate 2 times, party 2 times
        assert len(result) > 0
        assert result[0]["event_type"] == "wedding"
        assert result[0]["count"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# run_full_analysis
# ─────────────────────────────────────────────────────────────────────────────

class TestRunFullAnalysis:
    def setup_method(self):
        self.analyzer = KPIAnalyzer(db_path="/nonexistent/db.sqlite")

    def test_returns_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result, dict)

    def test_has_timestamp(self):
        result = self.analyzer.run_full_analysis()
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_has_all_8_kpi_keys(self):
        result = self.analyzer.run_full_analysis()
        expected_keys = [
            "kpi_1_orders_per_period",
            "kpi_2_conversion",
            "kpi_3_popular_categories",
            "kpi_4_client_return_rate",
            "kpi_5_deal_cycle_days",
            "kpi_6_model_ratings",
            "kpi_7_bot_activity",
            "kpi_8_top_requests",
        ]
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_kpi_1_is_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_1_orders_per_period"], dict)

    def test_kpi_2_is_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_2_conversion"], dict)

    def test_kpi_3_is_list(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_3_popular_categories"], list)

    def test_kpi_4_is_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_4_client_return_rate"], dict)

    def test_kpi_5_is_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_5_deal_cycle_days"], dict)

    def test_kpi_6_is_list(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_6_model_ratings"], list)

    def test_kpi_7_is_dict(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_7_bot_activity"], dict)

    def test_kpi_8_is_list(self):
        result = self.analyzer.run_full_analysis()
        assert isinstance(result["kpi_8_top_requests"], list)

    def test_timestamp_is_iso_format(self):
        result = self.analyzer.run_full_analysis()
        # Should parse without error
        datetime.datetime.fromisoformat(result["timestamp"])

    def test_no_db_returns_safe_defaults(self):
        result = self.analyzer.run_full_analysis()
        assert result["kpi_1_orders_per_period"]["total"] == 0
        assert result["kpi_2_conversion"]["conversion_rate"] == 0.0
        assert result["kpi_4_client_return_rate"]["return_rate"] == 0.0
        assert result["kpi_7_bot_activity"]["unique_users"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# generate_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSummary:
    def setup_method(self):
        self.analyzer = KPIAnalyzer(db_path="/nonexistent/db.sqlite")

    def test_returns_string(self):
        result = self.analyzer.generate_summary()
        assert isinstance(result, str)

    def test_contains_kpi_header(self):
        result = self.analyzer.generate_summary()
        assert "KPI" in result

    def test_contains_orders_keyword(self):
        result = self.analyzer.generate_summary()
        assert "Заявок" in result or "orders" in result.lower()

    def test_contains_conversion_keyword(self):
        result = self.analyzer.generate_summary()
        assert "Конверсия" in result or "%" in result

    def test_contains_client_return_keyword(self):
        result = self.analyzer.generate_summary()
        assert "Возврат" in result or "клиент" in result.lower()

    def test_contains_deal_cycle_keyword(self):
        result = self.analyzer.generate_summary()
        assert "цикл" in result.lower() or "дн" in result

    def test_contains_unique_users_keyword(self):
        result = self.analyzer.generate_summary()
        assert "клиент" in result.lower() or "пользовател" in result.lower()

    def test_accepts_pre_computed_analysis(self):
        analysis = {
            "kpi_1_orders_per_period": {"total": 42, "month": 10},
            "kpi_2_conversion": {"conversion_rate": 0.75},
            "kpi_4_client_return_rate": {"return_rate": 0.3},
            "kpi_5_deal_cycle_days": {"avg_days": 5.0},
            "kpi_7_bot_activity": {"unique_users": 99},
        }
        result = self.analyzer.generate_summary(analysis=analysis)
        assert "42" in result
        assert "75.0" in result
        assert "99" in result

    def test_multiline_output(self):
        result = self.analyzer.generate_summary()
        assert "\n" in result

    def test_is_non_empty(self):
        result = self.analyzer.generate_summary()
        assert len(result) > 20


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases and integration
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def setup_method(self):
        self.analyzer = KPIAnalyzer()

    def test_orders_per_period_with_empty_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders (id INTEGER, created_at TEXT, status TEXT, client_chat_id TEXT)")
        result = self.analyzer.analyze_orders_per_period(conn)
        conn.close()
        assert result["total"] == 0

    def test_conversion_with_empty_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
        result = self.analyzer.analyze_conversion_by_source(conn)
        conn.close()
        assert result["conversion_rate"] == 0.0

    def test_client_return_rate_with_single_client(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders (id INTEGER, client_chat_id TEXT, status TEXT)")
        conn.execute("INSERT INTO orders VALUES (1, 'only_one', 'new')")
        conn.commit()
        result = self.analyzer.analyze_client_return_rate(conn)
        conn.close()
        assert result["return_rate"] == 0.0
        assert result["total_clients"] == 1
        assert result["returning_clients"] == 0

    def test_deal_cycle_with_negative_days_excluded(self):
        """Orders where updated_at < created_at should be excluded."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE orders (
                id INTEGER, status TEXT, created_at TEXT, updated_at TEXT
            )
        """)
        # Intentionally reversed timestamps (updated before created) — should be filtered out
        conn.execute("INSERT INTO orders VALUES (1, 'completed', '2024-02-01', '2024-01-01')")
        conn.execute("INSERT INTO orders VALUES (2, 'completed', '2024-01-01', '2024-01-05')")
        conn.commit()
        result = self.analyzer.analyze_deal_cycle_days(conn)
        conn.close()
        # Only the valid record (id=2, 4 days) should be counted
        assert result["sample_size"] == 1
        assert result["avg_days"] == 4.0

    def test_top_requests_with_all_null_event_types(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders (id INTEGER, event_type TEXT)")
        conn.execute("INSERT INTO orders VALUES (1, NULL)")
        conn.execute("INSERT INTO orders VALUES (2, NULL)")
        conn.commit()
        result = self.analyzer.analyze_top_client_requests(conn)
        conn.close()
        assert result == []

    def test_full_analysis_with_real_in_memory_db(self, tmp_path):
        """Integration: create a real SQLite file and run full analysis."""
        db_file = tmp_path / "test_nevesty.db"
        conn = _make_in_memory_db()
        # Persist to file
        disk_conn = sqlite3.connect(str(db_file))
        for line in conn.iterdump():
            if line not in ('BEGIN;', 'COMMIT;'):
                try:
                    disk_conn.execute(line)
                except Exception:
                    pass
        disk_conn.commit()
        disk_conn.close()
        conn.close()

        analyzer = KPIAnalyzer(db_path=str(db_file))
        result = analyzer.run_full_analysis()

        assert result["kpi_1_orders_per_period"]["total"] == 8
        assert result["kpi_2_conversion"]["completed"] == 5
        assert len(result["kpi_3_popular_categories"]) >= 2
        assert result["kpi_4_client_return_rate"]["total_clients"] == 6
        assert result["kpi_5_deal_cycle_days"]["sample_size"] == 3
        assert len(result["kpi_6_model_ratings"]) >= 1
        assert result["kpi_7_bot_activity"]["unique_users"] == 6
        assert len(result["kpi_8_top_requests"]) >= 1
