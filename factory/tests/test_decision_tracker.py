"""Tests for DecisionTracker in agents/decision_tracker.py."""
import sys
import os
import pytest

# Make the factory package importable from the grandparent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from factory.agents.decision_tracker import DecisionTracker


@pytest.fixture
def tracker():
    return DecisionTracker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(title="Task", status="pending"):
    return {"title": title, "status": status}


# ===========================================================================
# TestGetExecutionSummary
# ===========================================================================

class TestGetExecutionSummary:

    def test_empty_list_done_count_zero(self, tracker):
        result = tracker.get_execution_summary([])
        assert result["done_count"] == 0

    def test_empty_list_in_progress_count_zero(self, tracker):
        result = tracker.get_execution_summary([])
        assert result["in_progress_count"] == 0

    def test_empty_list_execution_rate_zero(self, tracker):
        result = tracker.get_execution_summary([])
        assert result["execution_rate"] == 0

    def test_all_done_execution_rate_one(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "done"), make_task("C", "done")]
        result = tracker.get_execution_summary(tasks)
        assert result["execution_rate"] == 1.0

    def test_mixed_tasks_done_count(self, tracker):
        tasks = [
            make_task("A", "done"),
            make_task("B", "in_progress"),
            make_task("C", "pending"),
            make_task("D", "done"),
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["done_count"] == 2

    def test_mixed_tasks_in_progress_count(self, tracker):
        tasks = [
            make_task("A", "done"),
            make_task("B", "in_progress"),
            make_task("C", "pending"),
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["in_progress_count"] == 1

    def test_mixed_tasks_execution_rate(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "pending")]
        result = tracker.get_execution_summary(tasks)
        assert result["execution_rate"] == pytest.approx(0.5)

    def test_returns_all_required_keys(self, tracker):
        result = tracker.get_execution_summary([])
        required_keys = {"done_count", "in_progress_count", "pending_count", "execution_rate", "done_titles", "next_focus"}
        assert required_keys.issubset(result.keys())

    def test_done_titles_is_list(self, tracker):
        tasks = [make_task("X", "done")]
        result = tracker.get_execution_summary(tasks)
        assert isinstance(result["done_titles"], list)

    def test_done_titles_contains_strings(self, tracker):
        tasks = [make_task("Alpha", "done"), make_task("Beta", "done")]
        result = tracker.get_execution_summary(tasks)
        assert all(isinstance(t, str) for t in result["done_titles"])

    def test_done_titles_content(self, tracker):
        tasks = [make_task("Alpha", "done"), make_task("Beta", "in_progress")]
        result = tracker.get_execution_summary(tasks)
        assert result["done_titles"] == ["Alpha"]

    def test_next_focus_is_first_pending_title(self, tracker):
        tasks = [
            make_task("Done task", "done"),
            make_task("First pending", "pending"),
            make_task("Second pending", "pending"),
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == "First pending"

    def test_next_focus_is_none_string_when_no_pending(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "in_progress")]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == "none"

    def test_next_focus_none_when_empty(self, tracker):
        result = tracker.get_execution_summary([])
        assert result["next_focus"] == "none"

    def test_execution_rate_is_float(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "pending")]
        result = tracker.get_execution_summary(tasks)
        assert isinstance(result["execution_rate"], float)

    def test_execution_rate_between_zero_and_one(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "pending"), make_task("C", "in_progress")]
        result = tracker.get_execution_summary(tasks)
        assert 0.0 <= result["execution_rate"] <= 1.0

    def test_total_based_on_actual_task_count(self, tracker):
        tasks = [make_task("A", "done"), make_task("B", "done"), make_task("C", "pending")]
        result = tracker.get_execution_summary(tasks)
        # 2 done out of 3 total
        assert result["execution_rate"] == pytest.approx(2 / 3)

    def test_pending_count_correct(self, tracker):
        tasks = [
            make_task("A", "pending"),
            make_task("B", "pending"),
            make_task("C", "done"),
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["pending_count"] == 2


# ===========================================================================
# TestGenerateAccountabilityReport
# ===========================================================================

class TestGenerateAccountabilityReport:

    def _summary(self, execution_rate=0.5, done=2, in_progress=1, pending=1):
        return {
            "execution_rate": execution_rate,
            "done_count": done,
            "in_progress_count": in_progress,
            "pending_count": pending,
        }

    def test_returns_string(self, tracker):
        report = tracker.generate_accountability_report(self._summary())
        assert isinstance(report, str)

    def test_contains_execution_percentage(self, tracker):
        report = tracker.generate_accountability_report(self._summary(execution_rate=0.5))
        assert "50%" in report

    def test_contains_done_count(self, tracker):
        report = tracker.generate_accountability_report(self._summary(done=3))
        assert "3" in report

    def test_contains_in_progress_count(self, tracker):
        summary = self._summary(in_progress=4)
        report = tracker.generate_accountability_report(summary)
        assert "4" in report

    def test_high_execution_rate_shows_100(self, tracker):
        report = tracker.generate_accountability_report(self._summary(execution_rate=1.0))
        assert "100%" in report

    def test_zero_execution_shows_0_percent(self, tracker):
        report = tracker.generate_accountability_report(self._summary(execution_rate=0.0))
        assert "0%" in report

    def test_report_is_multiline(self, tracker):
        report = tracker.generate_accountability_report(self._summary())
        assert "\n" in report


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:

    def test_task_missing_title_uses_action(self, tracker):
        tasks = [{"action": "Deploy service", "status": "done"}]
        result = tracker.get_execution_summary(tasks)
        assert result["done_titles"] == ["Deploy service"]

    def test_task_missing_title_and_action_returns_empty_string(self, tracker):
        tasks = [{"status": "done"}]
        result = tracker.get_execution_summary(tasks)
        assert result["done_titles"] == [""]

    def test_task_missing_status_counted_correctly(self, tracker):
        # A task with no status won't match done/in_progress/pending — total still increases
        tasks = [{"title": "Ghost task"}]
        result = tracker.get_execution_summary(tasks)
        assert result["done_count"] == 0
        assert result["in_progress_count"] == 0
        assert result["pending_count"] == 0
        # execution_rate: 0 done / max(1, 1) = 0
        assert result["execution_rate"] == 0.0

    def test_none_title_falls_back_to_action(self, tracker):
        tasks = [{"title": None, "action": "Fallback action", "status": "pending"}]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == "Fallback action"

    def test_none_title_and_no_action_returns_empty_string_for_next_focus(self, tracker):
        tasks = [{"title": None, "status": "pending"}]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == ""

    def test_generate_report_with_empty_summary(self, tracker):
        # Should not crash; defaults to 0
        report = tracker.generate_accountability_report({})
        assert isinstance(report, str)
        assert "0%" in report
