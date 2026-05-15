"""Tests for CEO weekly/monthly report generation (БЛОК 5.3)."""
import os
import json
import sqlite3
import tempfile
import pytest

from factory.cycle import (
    _format_weekly_report,
    _format_monthly_report,
    run_phase_ceo_reports,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cycle(phases: dict | None = None) -> dict:
    return {"timestamp": "2026-01-01T00:00:00", "phases": phases or {}}


def _make_db_with_orders(orders: list[tuple]) -> str:
    """Create a temporary SQLite DB with an orders table and given rows."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY,"
        "  client_chat_id TEXT,"
        "  budget REAL,"
        "  status TEXT,"
        "  created_at TEXT"
        ")"
    )
    conn.executemany(
        "INSERT INTO orders (client_chat_id, budget, status, created_at) VALUES (?,?,?,?)",
        orders,
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# _format_weekly_report tests
# ---------------------------------------------------------------------------

class TestFormatWeeklyReport:
    def test_empty_returns_nonempty_string(self):
        result = _format_weekly_report([])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_returns_no_data_message(self):
        result = _format_weekly_report([])
        assert "Нет данных" in result

    def test_with_cycles_contains_header(self):
        cycles = [_make_cycle() for _ in range(3)]
        result = _format_weekly_report(cycles)
        assert "ЕЖЕНЕДЕЛЬНЫЙ" in result

    def test_cycle_count_is_correct(self):
        cycles = [_make_cycle() for _ in range(5)]
        result = _format_weekly_report(cycles)
        assert "5" in result

    def test_contains_success_rate(self):
        cycles = [_make_cycle() for _ in range(2)]
        result = _format_weekly_report(cycles)
        assert "Успешность:" in result

    def test_contains_recommendations(self):
        cycles = [_make_cycle()]
        result = _format_weekly_report(cycles)
        assert "Рекомендации" in result

    def test_highlights_ok_phases(self):
        cycles = [
            _make_cycle({"analytics": {"status": "ok"}}),
            _make_cycle({"sales": {"status": "ok"}}),
        ]
        result = _format_weekly_report(cycles)
        assert "✓" in result

    def test_error_phases_counted_in_errors(self):
        cycles = [
            _make_cycle({"analytics": {"status": "error"}}),
        ]
        result = _format_weekly_report(cycles)
        assert "Ошибок: 1" in result

    def test_no_error_phases_shows_zero_errors(self):
        cycles = [_make_cycle({"analytics": {"status": "ok"}})]
        result = _format_weekly_report(cycles)
        assert "Ошибок: 0" in result

    def test_returns_string_type(self):
        result = _format_weekly_report([_make_cycle()])
        assert isinstance(result, str)

    def test_multiline_output(self):
        cycles = [_make_cycle() for _ in range(3)]
        result = _format_weekly_report(cycles)
        assert "\n" in result


# ---------------------------------------------------------------------------
# _format_monthly_report tests
# ---------------------------------------------------------------------------

class TestFormatMonthlyReport:
    def test_empty_cycles_nonexistent_db_returns_nonempty(self):
        result = _format_monthly_report([], "/nonexistent/path/db.sqlite")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_monthly_header(self):
        result = _format_monthly_report([], "/nonexistent/db.sqlite")
        assert "ЕЖЕМЕСЯЧНЫЙ" in result

    def test_error_message_on_bad_db(self):
        result = _format_monthly_report([], "/nonexistent/db.sqlite")
        assert "Ошибка" in result

    def test_with_real_db_contains_orders(self):
        path = _make_db_with_orders([
            ("user1", 10000, "completed", "2026-05-01T00:00:00"),
            ("user2", 5000, "confirmed", "2026-05-02T00:00:00"),
        ])
        try:
            result = _format_monthly_report([], path)
            assert "Заявок за месяц:" in result
        finally:
            os.unlink(path)

    def test_with_real_db_contains_revenue(self):
        path = _make_db_with_orders([
            ("user1", 20000, "completed", "2026-05-10T00:00:00"),
        ])
        try:
            result = _format_monthly_report([], path)
            assert "Выручка" in result
        finally:
            os.unlink(path)

    def test_with_real_db_contains_clients(self):
        path = _make_db_with_orders([
            ("clientA", 5000, "completed", "2026-05-10T00:00:00"),
            ("clientB", 3000, "completed", "2026-05-11T00:00:00"),
        ])
        try:
            result = _format_monthly_report([], path)
            assert "клиент" in result.lower()
        finally:
            os.unlink(path)

    def test_strategic_goals_present(self):
        path = _make_db_with_orders([])
        try:
            result = _format_monthly_report([], path)
            assert "Стратегические цели" in result
        finally:
            os.unlink(path)

    def test_cycle_count_in_report(self):
        cycles = [_make_cycle() for _ in range(10)]
        result = _format_monthly_report(cycles, "/nonexistent/db.sqlite")
        assert "10" in result


# ---------------------------------------------------------------------------
# run_phase_ceo_reports tests
# ---------------------------------------------------------------------------

class TestRunPhaseCeoReports:
    def test_returns_dict(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert isinstance(result, dict)

    def test_status_ok(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert result.get("status") == "ok"

    def test_has_weekly_report_key(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert "weekly_report" in result

    def test_has_monthly_report_key(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert "monthly_report" in result

    def test_weekly_report_is_string(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert isinstance(result["weekly_report"], str)

    def test_monthly_report_is_string(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert isinstance(result["monthly_report"], str)

    def test_weekly_lines_positive_with_no_history(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        # Even with no data, _format_weekly_report returns a string with 1+ lines
        assert result.get("weekly_lines", 0) >= 1

    def test_monthly_lines_positive(self):
        result = run_phase_ceo_reports("/nonexistent.db")
        assert result.get("monthly_lines", 0) >= 1

    def test_with_history_path_nonexistent(self, tmp_path):
        missing = str(tmp_path / "missing_history.json")
        result = run_phase_ceo_reports("/nonexistent.db", history_path=missing)
        assert result.get("status") == "ok"

    def test_with_history_path_valid(self, tmp_path):
        history = [
            {"timestamp": "2026-01-01T00:00:00", "phases": {"analytics": {"status": "ok"}}},
            {"timestamp": "2026-01-02T00:00:00", "phases": {"sales": {"status": "ok"}}},
        ]
        hp = tmp_path / "factory_history.json"
        hp.write_text(json.dumps(history, ensure_ascii=False))
        result = run_phase_ceo_reports("/nonexistent.db", history_path=str(hp))
        assert result["status"] == "ok"
        assert "ЕЖЕНЕДЕЛЬНЫЙ" in result["weekly_report"]

    def test_cycles_loaded_from_history(self, tmp_path):
        history = [
            {"timestamp": f"2026-01-0{i}T00:00:00", "phases": {}}
            for i in range(1, 4)
        ]
        hp = tmp_path / "factory_history.json"
        hp.write_text(json.dumps(history, ensure_ascii=False))
        result = run_phase_ceo_reports("/nonexistent.db", history_path=str(hp))
        assert result.get("cycles_loaded") == 3
