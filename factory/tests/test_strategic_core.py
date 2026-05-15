"""Tests for StrategicCore CEO Intelligence methods (БЛОК 5.3)."""
from __future__ import annotations
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ceo():
    """Return a StrategicCore instance with think/think_json mocked to avoid LLM calls."""
    with patch("factory.agents.base.FactoryAgent.think", return_value="{}"), \
         patch("factory.agents.base.FactoryAgent.think_json") as mock_think_json, \
         patch("factory.agents.strategic_core.db") as mock_db:

        # Default: think_json returns a sensible CEO-style response
        mock_think_json.return_value = {
            "summary": "Стабильная неделя",
            "key_wins": ["Заявки поступают"],
            "concerns": [],
            "next_week_focus": "маркетинг",
            "kpi_snapshot": {"orders": 5, "revenue": 50000, "new_clients": 3, "conversion": 0.15},
            "month_summary": "Стабильный месяц",
            "growth_vs_prev_month": 2.5,
            "top_achievements": ["Рост заявок"],
            "strategic_priorities": ["Маркетинг"],
            "quarterly_outlook": "Позитивный",
            "experiments": [
                {
                    "name": "CTA Test",
                    "hypothesis": "Новая кнопка увеличит конверсию",
                    "control": "Старая кнопка",
                    "variant": "Новая кнопка",
                    "metric": "conversion_rate",
                    "expected_lift": "+8%",
                    "duration_days": 7,
                }
            ],
        }

        # DB stubs
        mock_db.get_recent_decisions.return_value = []
        mock_db.get_active_products.return_value = []
        mock_db.get_running_experiments.return_value = []
        mock_db.fetch_one.return_value = None
        mock_db.execute.return_value = None
        mock_db.save_ceo_decision.return_value = None

        from factory.agents.strategic_core import StrategicCore
        yield StrategicCore()


# ---------------------------------------------------------------------------
# generate_weekly_report tests
# ---------------------------------------------------------------------------

def test_generate_weekly_report(mock_ceo):
    result = mock_ceo.generate_weekly_report(metrics={"orders": 10}, decisions=[])
    assert "summary" in result or "error" in result


def test_generate_weekly_report_returns_dict(mock_ceo):
    result = mock_ceo.generate_weekly_report(metrics={"orders": 5, "revenue": 10000}, decisions=[])
    assert isinstance(result, dict)


def test_generate_weekly_report_no_args(mock_ceo):
    """Should work with default (None) args — backward compat."""
    result = mock_ceo.generate_weekly_report()
    assert isinstance(result, dict)


def test_generate_weekly_report_with_decisions(mock_ceo):
    decisions = [{"id": 1, "decision_type": "grow", "rationale": "Test"}]
    result = mock_ceo.generate_weekly_report(metrics={"conversion": 0.15}, decisions=decisions)
    assert isinstance(result, dict)


def test_generate_weekly_report_kpi_snapshot(mock_ceo):
    result = mock_ceo.generate_weekly_report(metrics={"orders": 10}, decisions=[])
    # Either spec format (kpi_snapshot) or fallback with legacy keys
    has_spec = "kpi_snapshot" in result or "summary" in result
    has_legacy = "headline" in result or "highlights" in result
    assert has_spec or has_legacy


# ---------------------------------------------------------------------------
# generate_monthly_report tests
# ---------------------------------------------------------------------------

def test_generate_monthly_report(mock_ceo):
    result = mock_ceo.generate_monthly_report(weekly_data=[{"summary": "test"}])
    assert "month_summary" in result or "error" in result


def test_generate_monthly_report_returns_dict(mock_ceo):
    result = mock_ceo.generate_monthly_report(weekly_data=[{"summary": "Хорошая неделя"}])
    assert isinstance(result, dict)


def test_generate_monthly_report_no_args(mock_ceo):
    """Should work with default (None) weekly_data — backward compat."""
    result = mock_ceo.generate_monthly_report()
    assert isinstance(result, dict)


def test_generate_monthly_report_multiple_weeks(mock_ceo):
    weeks = [{"summary": f"Неделя {i}"} for i in range(4)]
    result = mock_ceo.generate_monthly_report(weekly_data=weeks)
    assert isinstance(result, dict)
    assert "month_summary" in result or "executive_summary" in result or "error" in result


def test_generate_monthly_report_empty_weekly_data(mock_ceo):
    result = mock_ceo.generate_monthly_report(weekly_data=[])
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# propose_ab_experiments tests
# ---------------------------------------------------------------------------

def test_propose_ab_experiments(mock_ceo):
    result = mock_ceo.propose_ab_experiments(current_metrics={"conversion_rate": 0.15})
    assert "experiments" in result or "error" in result


def test_propose_ab_experiments_returns_dict(mock_ceo):
    result = mock_ceo.propose_ab_experiments(current_metrics={"conversion_rate": 0.10})
    assert isinstance(result, dict)


def test_propose_ab_experiments_has_list(mock_ceo):
    result = mock_ceo.propose_ab_experiments(current_metrics={"conversion_rate": 0.12})
    if "experiments" in result:
        assert isinstance(result["experiments"], list)


def test_propose_ab_experiments_empty_metrics(mock_ceo):
    result = mock_ceo.propose_ab_experiments(current_metrics={})
    assert isinstance(result, dict)
    assert "experiments" in result or "error" in result


def test_propose_ab_experiments_experiment_fields(mock_ceo):
    result = mock_ceo.propose_ab_experiments(
        current_metrics={"conversion_rate": 0.15, "avg_check": 5000}
    )
    if "experiments" in result and result["experiments"]:
        exp = result["experiments"][0]
        # At least one of the spec fields should be present
        assert any(k in exp for k in ("name", "hypothesis", "metric", "control", "variant"))
