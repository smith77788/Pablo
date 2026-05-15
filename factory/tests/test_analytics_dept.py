"""Tests for Analytics Department agents (БЛОК 5.2)."""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from unittest.mock import patch, MagicMock

from factory.agents.analytics_dept import (
    DataAnalyst,
    ConversionAnalyst,
    ExperimentEvaluator,
    KPITracker,
    AnalyticsDepartment,
)

# ── DataAnalyst ───────────────────────────────────────────────────────────────

class TestDataAnalyst:
    def setup_method(self):
        self.agent = DataAnalyst()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "analytics"

    def test_role_attribute(self):
        assert self.agent.role == "data_analyst"

    def test_analyze_trends_returns_dict_when_think_empty(self):
        with patch.object(self.agent, 'think', return_value=""):
            result = self.agent.analyze_trends({"visitors": 100})
        assert isinstance(result, dict)

    def test_analyze_trends_returns_dict_on_valid_json(self):
        payload = '{"trends": [], "anomalies": [], "recommendations": ["focus"]}'
        with patch.object(self.agent, 'think', return_value=payload):
            result = self.agent.analyze_trends({})
        assert isinstance(result, dict)
        assert "trends" in result

    def test_run_returns_dict(self):
        result = self.agent.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self):
        result = self.agent.run({})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_run_has_timestamp(self):
        result = self.agent.run({})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_run_with_none_context(self):
        result = self.agent.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_run_reflects_kpi_data(self):
        ctx = {"nevesty_kpis": {"total_users": 500, "total_orders": 42}}
        result = self.agent.run(ctx)
        full_text = " ".join(result["insights"])
        assert "500" in full_text or "42" in full_text

    def test_run_has_recommendations(self):
        result = self.agent.run({})
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)


# ── ConversionAnalyst ─────────────────────────────────────────────────────────

class TestConversionAnalyst:
    def setup_method(self):
        self.agent = ConversionAnalyst()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "analytics"

    def test_role_attribute(self):
        assert self.agent.role == "conversion_analyst"

    def test_find_conversion_leaks_returns_dict_on_empty_think(self):
        with patch.object(self.agent, 'think', return_value=""):
            result = self.agent.find_conversion_leaks({"visits": 1000, "bookings": 30})
        assert isinstance(result, dict)

    def test_find_conversion_leaks_parses_json(self):
        payload = '{"leaks": [{"stage": "catalog", "drop_rate": "60%", "fix": "add CTA"}], "quick_win": "CTA", "expected_lift": "10%"}'
        with patch.object(self.agent, 'think', return_value=payload):
            result = self.agent.find_conversion_leaks({})
        assert "leaks" in result

    def test_run_returns_dict(self):
        result = self.agent.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self):
        result = self.agent.run({})
        assert "insights" in result
        assert len(result["insights"]) > 0

    def test_run_has_timestamp(self):
        result = self.agent.run({})
        assert "timestamp" in result

    def test_run_with_none_context(self):
        result = self.agent.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_run_computes_conversion_rate(self):
        ctx = {"nevesty_kpis": {"total_users": 200, "total_orders": 10}}
        result = self.agent.run(ctx)
        # conversion = 10/200*100 = 5.0%, should mention '5.0' in insights
        full_text = " ".join(result["insights"])
        assert "5.0" in full_text

    def test_run_zero_users_does_not_crash(self):
        ctx = {"nevesty_kpis": {"total_users": 0, "total_orders": 0}}
        result = self.agent.run(ctx)
        assert isinstance(result, dict)


# ── ExperimentEvaluator ───────────────────────────────────────────────────────

class TestExperimentEvaluator:
    def setup_method(self):
        self.agent = ExperimentEvaluator()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "analytics"

    def test_role_attribute(self):
        assert self.agent.role == "experiment_evaluator"

    def test_evaluate_returns_dict_on_empty_think(self):
        with patch.object(self.agent, 'think', return_value=""):
            result = self.agent.evaluate({"id": 1, "conversion_lift": 3.5})
        assert isinstance(result, dict)

    def test_evaluate_parses_json(self):
        payload = '{"decision": "scale", "confidence": "high", "reasoning": "ok", "next_step": "deploy"}'
        with patch.object(self.agent, 'think', return_value=payload):
            result = self.agent.evaluate({})
        assert result.get("decision") == "scale"

    def test_run_returns_dict(self):
        result = self.agent.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self):
        result = self.agent.run({})
        assert "insights" in result
        assert len(result["insights"]) > 0

    def test_run_has_timestamp(self):
        result = self.agent.run({})
        assert "timestamp" in result

    def test_run_with_none_context(self):
        result = self.agent.run(None)
        assert isinstance(result, dict)
        assert "insights" in result


# ── KPITracker ────────────────────────────────────────────────────────────────

class TestKPITracker:
    def setup_method(self):
        self.agent = KPITracker()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "analytics"

    def test_role_attribute(self):
        assert self.agent.role == "kpi_tracker"

    def test_generate_kpi_report_returns_dict_on_empty_think(self):
        with patch.object(self.agent, 'think', return_value=""):
            result = self.agent.generate_kpi_report({}, {"orders_target": 100})
        assert isinstance(result, dict)

    def test_generate_kpi_report_parses_json(self):
        payload = '{"overall_health": "green", "kpis": [], "alert": null, "action_needed": "none"}'
        with patch.object(self.agent, 'think', return_value=payload):
            result = self.agent.generate_kpi_report({}, {})
        assert result.get("overall_health") == "green"

    def test_run_returns_dict(self):
        result = self.agent.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self):
        result = self.agent.run({})
        assert "insights" in result
        assert len(result["insights"]) > 0

    def test_run_has_timestamp(self):
        result = self.agent.run({})
        assert "timestamp" in result

    def test_run_with_none_context(self):
        result = self.agent.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_run_reflects_orders_kpi(self):
        ctx = {"nevesty_kpis": {"total_orders": 120}}
        result = self.agent.run(ctx)
        full_text = " ".join(result["insights"])
        assert "120" in full_text

    def test_run_shows_completed_status_when_over_target(self):
        ctx = {"nevesty_kpis": {"total_orders": 150}}
        result = self.agent.run(ctx)
        full_text = " ".join(result["insights"])
        assert "Выполнено" in full_text or "✅" in full_text

    def test_run_shows_warning_when_under_target(self):
        ctx = {"nevesty_kpis": {"total_orders": 30}}
        result = self.agent.run(ctx)
        full_text = " ".join(result["insights"])
        assert "Недовыполнение" in full_text or "⚠️" in full_text


# ── AnalyticsDepartment ───────────────────────────────────────────────────────

class TestAnalyticsDepartment:
    def setup_method(self):
        self.dept = AnalyticsDepartment()

    def test_instantiation(self):
        assert self.dept is not None

    def test_has_analyst(self):
        assert isinstance(self.dept.analyst, DataAnalyst)

    def test_has_conversion(self):
        assert isinstance(self.dept.conversion, ConversionAnalyst)

    def test_has_evaluator(self):
        assert isinstance(self.dept.evaluator, ExperimentEvaluator)

    def test_has_kpi(self):
        assert isinstance(self.dept.kpi, KPITracker)

    def test_execute_task_returns_dict(self):
        result = self.dept.execute_task("analyze data", {})
        assert isinstance(result, dict)

    def test_execute_task_has_health_score(self):
        result = self.dept.execute_task("report", {})
        assert "health_score" in result
        assert isinstance(result["health_score"], int)

    def test_execute_task_has_recommended_focus(self):
        result = self.dept.execute_task("report", {})
        assert "recommended_focus" in result
        assert isinstance(result["recommended_focus"], str)
        assert len(result["recommended_focus"]) > 0

    def test_execute_task_has_insights(self):
        result = self.dept.execute_task("analyze", {})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_execute_task_has_timestamp(self):
        result = self.dept.execute_task("test", {})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_execute_task_has_department_field(self):
        result = self.dept.execute_task("test", {})
        assert result.get("department") == "analytics"

    def test_execute_task_has_task_field(self):
        result = self.dept.execute_task("my task", {})
        assert result.get("task") == "my task"

    def test_execute_task_none_context_does_not_crash(self):
        result = self.dept.execute_task("test", None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_execute_task_has_roles_used(self):
        result = self.dept.execute_task("analyze", {})
        assert "roles_used" in result
        assert len(result["roles_used"]) >= 2

    def test_execute_task_always_has_data_analyst(self):
        result = self.dept.execute_task("anything", {})
        assert "data_analyst" in result["roles_used"]

    def test_execute_task_always_has_kpi_tracker(self):
        result = self.dept.execute_task("anything", {})
        assert "kpi_tracker" in result["roles_used"]

    def test_execute_task_conversion_task_activates_analyst(self):
        result = self.dept.execute_task("improve conversion funnel", {})
        assert "conversion_analyst" in result["roles_used"]

    def test_execute_task_experiment_task_activates_evaluator(self):
        result = self.dept.execute_task("evaluate A/B test results", {})
        assert "experiment_evaluator" in result["roles_used"]

    def test_execute_task_health_score_green_on_high_orders(self):
        ctx = {"nevesty_kpis": {"total_orders": 150}}
        result = self.dept.execute_task("report", ctx)
        assert result["health_score"] == 80

    def test_execute_task_health_score_red_on_low_orders(self):
        ctx = {"nevesty_kpis": {"total_orders": 5}}
        result = self.dept.execute_task("report", ctx)
        assert result["health_score"] == 25

    def test_execute_task_health_score_yellow_default(self):
        ctx = {"nevesty_kpis": {"total_orders": 50}}
        result = self.dept.execute_task("report", ctx)
        assert result["health_score"] == 50

    def test_execute_task_empty_task_string(self):
        result = self.dept.execute_task("", {})
        assert isinstance(result, dict)
        assert "insights" in result

    def test_run_full_analysis_returns_dict(self):
        """run_full_analysis with mocked think (no real API)."""
        with patch.object(DataAnalyst, 'think', return_value="{}"), \
             patch.object(ConversionAnalyst, 'think', return_value="{}"), \
             patch.object(ExperimentEvaluator, 'think', return_value="{}"), \
             patch.object(KPITracker, 'think', return_value="{}"), \
             patch('factory.db.execute') as mock_db:
            result = self.dept.run_full_analysis({}, [])
        assert isinstance(result, dict)

    def test_run_full_analysis_has_health_score(self):
        with patch.object(DataAnalyst, 'think', return_value="{}"), \
             patch.object(ConversionAnalyst, 'think', return_value="{}"), \
             patch.object(ExperimentEvaluator, 'think', return_value="{}"), \
             patch.object(KPITracker, 'think', return_value="{}"), \
             patch('factory.db.execute'):
            result = self.dept.run_full_analysis({}, [])
        assert "health_score" in result
        assert isinstance(result["health_score"], int)

    def test_run_full_analysis_has_recommended_focus(self):
        with patch.object(DataAnalyst, 'think', return_value="{}"), \
             patch.object(ConversionAnalyst, 'think', return_value="{}"), \
             patch.object(ExperimentEvaluator, 'think', return_value="{}"), \
             patch.object(KPITracker, 'think', return_value="{}"), \
             patch('factory.db.execute'):
            result = self.dept.run_full_analysis({}, [])
        assert "recommended_focus" in result
        assert isinstance(result["recommended_focus"], str)

    def test_run_full_analysis_with_experiments(self):
        experiments = [{"id": 1, "name": "button_test"}]
        with patch.object(DataAnalyst, 'think', return_value="{}"), \
             patch.object(ConversionAnalyst, 'think', return_value="{}"), \
             patch.object(ExperimentEvaluator, 'think', return_value='{"decision": "iterate", "confidence": "medium", "reasoning": "ok", "next_step": "continue"}'), \
             patch.object(KPITracker, 'think', return_value="{}"), \
             patch('factory.db.execute'):
            result = self.dept.run_full_analysis({}, experiments)
        assert "experiment_evaluations" in result
        assert len(result["experiment_evaluations"]) == 1
        assert result["experiment_evaluations"][0]["experiment_id"] == 1
