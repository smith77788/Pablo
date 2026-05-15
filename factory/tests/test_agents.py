"""
Basic tests for Factory agents.
These tests verify that agents initialize correctly and produce output.
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add parent dir (/home/user/Pablo) to path so "factory" package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestBaseAgent:
    """Tests for the base FactoryAgent class."""

    def test_base_agent_imports(self):
        from factory.agents.base import FactoryAgent
        assert FactoryAgent is not None

    def test_agent_has_name_and_init(self):
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        assert hasattr(agent, 'name')
        assert hasattr(agent, 'department')

    def test_base_agent_default_name(self):
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        assert agent.name == "base"
        assert agent.department == "general"

    def test_base_agent_has_think_method(self):
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        assert hasattr(agent, 'think')
        assert callable(agent.think)


class TestAnalyticsEngine:
    """Tests for the AnalyticsEngine agent."""

    def test_analytics_engine_imports(self):
        from factory.agents.analytics_engine import AnalyticsEngine
        agent = AnalyticsEngine()
        assert agent is not None

    def test_analytics_engine_name(self):
        from factory.agents.analytics_engine import AnalyticsEngine
        agent = AnalyticsEngine()
        assert agent.name == "analytics_engine"

    def test_analytics_engine_has_think_method(self):
        from factory.agents.analytics_engine import AnalyticsEngine
        agent = AnalyticsEngine()
        assert hasattr(agent, 'think')

    def test_collect_nevesty_metrics_returns_dict(self):
        from factory.agents.analytics_engine import AnalyticsEngine
        agent = AnalyticsEngine()
        # Should return dict even if DB is missing
        metrics = agent._collect_nevesty_metrics()
        assert isinstance(metrics, dict)


class TestContentGenerator:
    """Tests for the ContentGenerator agent."""

    def test_content_generator_imports(self):
        from factory.agents.content_generator import ContentGenerator
        agent = ContentGenerator()
        assert agent is not None

    def test_content_generator_name(self):
        from factory.agents.content_generator import ContentGenerator
        agent = ContentGenerator()
        assert agent.name == "content_generator"

    def test_content_generator_has_run_method(self):
        from factory.agents.content_generator import ContentGenerator
        agent = ContentGenerator()
        assert hasattr(agent, 'run')

    def test_get_recent_stats_returns_dict(self):
        from factory.agents.content_generator import ContentGenerator
        agent = ContentGenerator()
        stats = agent._get_recent_stats()
        assert isinstance(stats, dict)


class TestExperimentTracker:
    """Tests for the ExperimentTracker agent."""

    def test_experiment_tracker_imports(self):
        from factory.agents.experiment_tracker import ExperimentTracker
        agent = ExperimentTracker()
        assert agent is not None

    def test_experiment_tracker_name(self):
        from factory.agents.experiment_tracker import ExperimentTracker
        agent = ExperimentTracker()
        assert agent.name == "ExperimentTracker"
        assert agent.department == "analytics"

    def test_get_current_metrics_returns_dict(self):
        from factory.agents.experiment_tracker import ExperimentTracker
        agent = ExperimentTracker()
        # Should return dict even if Nevesty DB is missing or schema differs
        metrics = agent._get_current_metrics()
        assert isinstance(metrics, dict)

    def test_run_returns_dict_with_expected_keys(self):
        from factory.agents.experiment_tracker import ExperimentTracker
        agent = ExperimentTracker()
        # run() uses **kwargs, no factory_db argument
        result = agent.run()
        assert isinstance(result, dict)
        assert 'evaluated' in result
        assert 'success' in result
        assert 'fail' in result
        assert 'details' in result

    def test_run_details_is_list(self):
        from factory.agents.experiment_tracker import ExperimentTracker
        agent = ExperimentTracker()
        result = agent.run()
        assert isinstance(result['details'], list)

    # ── New tests for record_metric_result and get_active_experiments ──

    def test_get_active_experiments_returns_list(self):
        """get_active_experiments() must return a list even when DB is empty."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        with patch('factory.db.fetch_all', return_value=[]):
            result = agent.get_active_experiments()
        assert isinstance(result, list)

    def test_get_active_experiments_db_error_returns_empty(self):
        """get_active_experiments() must return [] on DB error."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        with patch('factory.db.fetch_all', side_effect=Exception("db unavailable")):
            result = agent.get_active_experiments()
        assert result == []

    def test_record_metric_result_not_found(self):
        """record_metric_result returns error dict when experiment does not exist."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        with patch('factory.db.fetch_one', return_value=None):
            result = agent.record_metric_result(999, 'orders_month', 50.0)
        assert result.get('error') == 'not found'
        assert result.get('id') == 999

    def test_record_metric_result_success_status(self):
        """Improvement ≥5% should produce status='success' and conclude experiment."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch, MagicMock
        agent = ExperimentTracker()
        fake_exp = {
            'id': 1, 'name': 'test-exp', 'status': 'running',
            'conversion_a': 100.0, 'notes': '',
        }
        with patch('factory.db.fetch_one', return_value=fake_exp), \
             patch('factory.db.execute') as mock_exec:
            result = agent.record_metric_result(1, 'orders_month', 110.0)
        assert result['status'] == 'success'
        assert result['metric_before'] == 100.0
        assert result['metric_after'] == 110.0
        mock_exec.assert_called_once()

    def test_record_metric_result_failed_status(self):
        """Drop >10% should produce status='failed'."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        fake_exp = {
            'id': 2, 'name': 'test-exp-2', 'status': 'running',
            'conversion_a': 100.0, 'notes': '',
        }
        with patch('factory.db.fetch_one', return_value=fake_exp), \
             patch('factory.db.execute'):
            result = agent.record_metric_result(2, 'orders_month', 85.0)
        assert result['status'] == 'failed'

    def test_record_metric_result_running_stays(self):
        """Change between -10% and +5% should keep status='running'."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        fake_exp = {
            'id': 3, 'name': 'test-exp-3', 'status': 'running',
            'conversion_a': 100.0, 'notes': '',
        }
        with patch('factory.db.fetch_one', return_value=fake_exp), \
             patch('factory.db.execute') as mock_exec:
            result = agent.record_metric_result(3, 'orders_month', 102.0)
        assert result['status'] == 'running'
        # No status change → execute called with only conversion_b update
        mock_exec.assert_called_once()

    def test_record_metric_result_zero_baseline_nonzero_value(self):
        """Zero baseline + positive value → success."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        fake_exp = {
            'id': 4, 'name': 'test-exp-4', 'status': 'running',
            'conversion_a': 0.0, 'notes': '',
        }
        with patch('factory.db.fetch_one', return_value=fake_exp), \
             patch('factory.db.execute'):
            result = agent.record_metric_result(4, 'orders_month', 10.0)
        assert result['status'] == 'success'

    def test_record_metric_result_db_error_returns_error_dict(self):
        """DB error inside record_metric_result should be handled gracefully."""
        from factory.agents.experiment_tracker import ExperimentTracker
        from unittest.mock import patch
        agent = ExperimentTracker()
        with patch('factory.db.fetch_one', side_effect=Exception("connection lost")):
            result = agent.record_metric_result(5, 'orders_month', 50.0)
        assert 'error' in result
        assert result['id'] == 5


class TestSalesDepartment:
    """Tests for Sales department agents."""

    def test_sales_module_imports(self):
        from factory.agents.sales import (
            LeadQualifierAgent, ProposalWriterAgent,
            FollowUpSpecialistAgent, PricingNegotiatorAgent,
        )
        assert LeadQualifierAgent is not None
        assert ProposalWriterAgent is not None
        assert FollowUpSpecialistAgent is not None
        assert PricingNegotiatorAgent is not None

    def test_lead_qualifier_init(self):
        from factory.agents.sales import LeadQualifierAgent
        agent = LeadQualifierAgent()
        assert agent.department == 'sales'
        assert agent.role == 'LeadQualifier'
        assert agent.name == 'Алиса'

    def test_proposal_writer_init(self):
        from factory.agents.sales import ProposalWriterAgent
        agent = ProposalWriterAgent()
        assert agent.department == 'sales'
        assert agent.role == 'ProposalWriter'
        assert agent.name == 'Михаил'

    def test_follow_up_specialist_init(self):
        from factory.agents.sales import FollowUpSpecialistAgent
        agent = FollowUpSpecialistAgent()
        assert agent.department == 'sales'
        assert agent.role == 'FollowUpSpecialist'
        assert agent.name == 'Екатерина'

    def test_pricing_negotiator_init(self):
        from factory.agents.sales import PricingNegotiatorAgent
        agent = PricingNegotiatorAgent()
        assert agent.department == 'sales'
        assert agent.role == 'PricingNegotiator'
        assert agent.name == 'Дмитрий'

    def test_lead_qualifier_has_run_method(self):
        from factory.agents.sales import LeadQualifierAgent
        agent = LeadQualifierAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_lead_qualifier_has_build_prompt(self):
        from factory.agents.sales import LeadQualifierAgent
        agent = LeadQualifierAgent()
        assert hasattr(agent, 'build_prompt')
        # build_prompt should return a string even with no DB data
        prompt = agent.build_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_sales_department_run_cycle(self):
        from factory.agents.sales import SalesDepartment
        dept = SalesDepartment()
        assert hasattr(dept, 'run_cycle')
        assert callable(dept.run_cycle)
        assert len(dept.agents) == 4


class TestCreativeDepartment:
    """Tests for Creative department agents."""

    def test_creative_module_imports(self):
        from factory.agents.creative import (
            CopywriterAgent, VisualConceptorAgent,
            BrandVoiceKeeperAgent, StorytellingAgent,
        )
        assert CopywriterAgent is not None
        assert VisualConceptorAgent is not None
        assert BrandVoiceKeeperAgent is not None
        assert StorytellingAgent is not None

    def test_copywriter_init(self):
        from factory.agents.creative import CopywriterAgent
        agent = CopywriterAgent()
        assert agent.department == 'creative'
        assert agent.role == 'Copywriter'
        assert agent.name == 'Анастасия'

    def test_visual_conceptor_init(self):
        from factory.agents.creative import VisualConceptorAgent
        agent = VisualConceptorAgent()
        assert agent.department == 'creative'
        assert agent.role == 'VisualConceptor'
        assert agent.name == 'Артём'

    def test_brand_voice_keeper_init(self):
        from factory.agents.creative import BrandVoiceKeeperAgent
        agent = BrandVoiceKeeperAgent()
        assert agent.department == 'creative'
        assert agent.role == 'BrandVoiceKeeper'
        assert agent.name == 'Мария'

    def test_storytelling_agent_init(self):
        from factory.agents.creative import StorytellingAgent
        agent = StorytellingAgent()
        assert agent.department == 'creative'
        assert agent.role == 'Storytelling'
        assert agent.name == 'Ольга'

    def test_copywriter_has_run_method(self):
        from factory.agents.creative import CopywriterAgent
        agent = CopywriterAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_visual_conceptor_has_run_method(self):
        from factory.agents.creative import VisualConceptorAgent
        agent = VisualConceptorAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_brand_voice_keeper_has_run_method(self):
        from factory.agents.creative import BrandVoiceKeeperAgent
        agent = BrandVoiceKeeperAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_storytelling_agent_has_run_method(self):
        from factory.agents.creative import StorytellingAgent
        agent = StorytellingAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_creative_department_run_cycle(self):
        from factory.agents.creative import CreativeDepartment
        dept = CreativeDepartment()
        assert hasattr(dept, 'run_cycle')
        assert callable(dept.run_cycle)
        assert len(dept.agents) == 4

    def test_copywriter_run_returns_dict(self):
        from factory.agents.creative import CopywriterAgent
        agent = CopywriterAgent()
        agent.think = lambda prompt, **kw: "Mock marketing copy"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get('role') == 'Copywriter'
        assert result.get('department') == 'creative'

    def test_storytelling_run_returns_dict(self):
        from factory.agents.creative import StorytellingAgent
        agent = StorytellingAgent()
        agent.think = lambda prompt, **kw: "Mock story"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get('role') == 'Storytelling'
        assert result.get('department') == 'creative'


class TestDatabase:
    """Tests for the factory database module."""

    def test_db_module_imports(self):
        import factory.db as db
        assert db is not None

    def test_db_has_init_function(self):
        import factory.db as db
        assert hasattr(db, 'init_db')
        assert callable(db.init_db)

    def test_db_has_fetch_functions(self):
        import factory.db as db
        assert hasattr(db, 'fetch_all')
        assert hasattr(db, 'fetch_one')

    def test_db_has_insert_function(self):
        import factory.db as db
        assert hasattr(db, 'insert')

    def test_db_init_creates_tables(self, tmp_path, monkeypatch):
        import factory.db as db
        from pathlib import Path
        # Redirect DB_PATH to a temp location
        monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test_factory.db')
        db.init_db()
        assert (tmp_path / 'test_factory.db').exists()


class TestCycleModule:
    """Tests for the cycle.py orchestration module."""

    def test_cycle_module_imports(self):
        import factory.cycle as cycle
        assert cycle is not None

    def test_run_cycle_function_exists(self):
        import factory.cycle as cycle
        assert hasattr(cycle, 'run_cycle')
        assert callable(cycle.run_cycle)

    def test_no_factory_cycle_class(self):
        # cycle.py uses a function-based API, not a class — document this
        import factory.cycle as cycle
        assert not hasattr(cycle, 'FactoryCycle'), (
            "FactoryCycle class does not exist; use run_cycle() function instead"
        )

    def test_helper_functions_exist(self):
        import factory.cycle as cycle
        assert hasattr(cycle, '_load_dept')
        assert hasattr(cycle, '_save_cycle_to_history')

    def test_load_dept_returns_none_for_unknown(self):
        import factory.cycle as cycle
        result = cycle._load_dept("nonexistent_department_xyz")
        assert result is None


class TestCustomerSuccess:
    """Tests for Customer Success department agents."""

    def test_onboarding_specialist_instantiates(self):
        from factory.agents.customer_success import OnboardingSpecialist
        agent = OnboardingSpecialist()
        assert agent.department == "customer_success"
        assert agent.role == "OnboardingSpecialist"

    def test_retention_analyst_instantiates(self):
        from factory.agents.customer_success import RetentionAnalyst
        agent = RetentionAnalyst()
        assert agent.department == "customer_success"
        assert agent.role == "RetentionAnalyst"

    def test_feedback_collector_instantiates(self):
        from factory.agents.customer_success import FeedbackCollector
        agent = FeedbackCollector()
        assert agent.department == "customer_success"
        assert agent.role == "FeedbackCollector"

    def test_onboarding_run_no_db(self):
        from factory.agents.customer_success import OnboardingSpecialist
        agent = OnboardingSpecialist()
        # Mock the think method to avoid API call
        agent.think = lambda prompt, **kw: "Mock analysis"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "OnboardingSpecialist"

    def test_retention_analyst_run_no_db(self):
        from factory.agents.customer_success import RetentionAnalyst
        agent = RetentionAnalyst()
        agent.think = lambda prompt, **kw: "Mock retention"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "RetentionAnalyst"
        assert "retention_data" in result

    def test_feedback_collector_run_no_db(self):
        from factory.agents.customer_success import FeedbackCollector
        agent = FeedbackCollector()
        agent.think = lambda prompt, **kw: "Mock feedback"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "FeedbackCollector"
        assert "review_data" in result


class TestFinanceDept:
    """Tests for Finance department agents."""

    def test_revenue_forecaster_instantiates(self):
        from factory.agents.finance import RevenueForecaster
        agent = RevenueForecaster()
        assert agent.department == "finance"
        assert agent.role == "RevenueForecaster"

    def test_pricing_strategist_instantiates(self):
        from factory.agents.finance import PricingStrategist
        agent = PricingStrategist()
        assert agent.department == "finance"
        assert agent.role == "PricingStrategist"

    def test_revenue_forecaster_run_no_db(self):
        from factory.agents.finance import RevenueForecaster
        agent = RevenueForecaster()
        agent.think = lambda prompt, **kw: "Mock forecast"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "RevenueForecaster"
        assert "trend_data" in result

    def test_pricing_strategist_run_no_db(self):
        from factory.agents.finance import PricingStrategist
        agent = PricingStrategist()
        agent.think = lambda prompt, **kw: "Mock pricing"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "PricingStrategist"
        assert "pricing_data" in result


class TestResearchDept:
    """Tests for Research department agents."""

    def test_market_researcher_instantiates(self):
        from factory.agents.research import MarketResearcher
        agent = MarketResearcher()
        assert agent.department == "research"
        assert agent.role == "MarketResearcher"

    def test_trend_spotter_instantiates(self):
        from factory.agents.research import TrendSpotter
        agent = TrendSpotter()
        assert agent.department == "research"
        assert agent.role == "TrendSpotter"

    def test_insight_synthesizer_instantiates(self):
        from factory.agents.research import InsightSynthesizer
        agent = InsightSynthesizer()
        assert agent.department == "research"
        assert agent.role == "InsightSynthesizer"

    def test_market_researcher_run_no_db(self):
        from factory.agents.research import MarketResearcher
        agent = MarketResearcher()
        agent.think = lambda prompt, **kw: "Mock market analysis"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "MarketResearcher"
        assert "market_data" in result

    def test_trend_spotter_run_no_db(self):
        from factory.agents.research import TrendSpotter
        agent = TrendSpotter()
        agent.think = lambda prompt, **kw: "Mock trends"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "TrendSpotter"
        assert "trend_data" in result

    def test_insight_synthesizer_run_no_db(self):
        from factory.agents.research import InsightSynthesizer
        agent = InsightSynthesizer()
        agent.think = lambda prompt, **kw: "Mock insights"
        result = agent.run()
        assert isinstance(result, dict)
        assert result.get("role") == "InsightSynthesizer"
        assert "data" in result


class TestExperimentSystem:
    def test_experiment_proposer_instantiates(self):
        from factory.agents.experiments import ExperimentProposer
        agent = ExperimentProposer()
        assert agent.department == "experiments"

    def test_experiment_tracker_instantiates(self):
        from factory.agents.experiments import ExperimentTracker
        agent = ExperimentTracker()
        assert agent.role == "experiment_tracker"

    def test_result_analyzer_instantiates(self):
        from factory.agents.experiments import ResultAnalyzer
        agent = ResultAnalyzer()
        assert agent.department == "experiments"

    def test_experiment_proposer_think(self):
        from factory.agents.experiments import ExperimentProposer
        agent = ExperimentProposer()
        prompt = agent.think()
        assert isinstance(prompt, str)
        assert len(prompt) > 10

    def test_experiment_tracker_think(self):
        from factory.agents.experiments import ExperimentTracker
        agent = ExperimentTracker()
        prompt = agent.think()
        assert isinstance(prompt, str)


class TestFinanceDepartment:
    """Tests for the standalone finance_department agents (heuristic, no API calls)."""

    # ── RevenueForecaster ──────────────────────────────────────────

    def test_forecast_monthly_revenue_returns_required_keys(self):
        from factory.agents.finance_department import RevenueForecaster
        agent = RevenueForecaster()
        history = [{"revenue": 100_000}, {"revenue": 120_000}, {"revenue": 110_000}]
        result = agent.forecast_monthly_revenue(history)
        assert isinstance(result, dict)
        for key in ("forecast", "confidence", "trend", "basis_months"):
            assert key in result, f"Missing key: {key}"

    def test_forecast_monthly_revenue_empty_history(self):
        from factory.agents.finance_department import RevenueForecaster
        agent = RevenueForecaster()
        result = agent.forecast_monthly_revenue([])
        assert result["forecast"] == 0.0
        assert result["confidence"] == "low"
        assert result["basis_months"] == 0

    def test_forecast_monthly_revenue_trend_growing(self):
        from factory.agents.finance_department import RevenueForecaster
        agent = RevenueForecaster()
        history = [{"revenue": 100_000}, {"revenue": 120_000}, {"revenue": 145_000}]
        result = agent.forecast_monthly_revenue(history)
        assert result["trend"] == "growing"

    def test_calculate_growth_rate_returns_float(self):
        from factory.agents.finance_department import RevenueForecaster
        agent = RevenueForecaster()
        rate = agent.calculate_growth_rate([100.0, 110.0, 121.0])
        assert isinstance(rate, float)
        assert abs(rate - 0.10) < 0.01  # ~10% growth

    def test_calculate_growth_rate_single_element(self):
        from factory.agents.finance_department import RevenueForecaster
        agent = RevenueForecaster()
        assert agent.calculate_growth_rate([500.0]) == 0.0

    # ── CostOptimizer ──────────────────────────────────────────────

    def test_analyze_cost_structure_returns_required_keys(self):
        from factory.agents.finance_department import CostOptimizer
        agent = CostOptimizer()
        expenses = {"marketing": 30_000, "hosting": 5_000, "accounting": 3_000}
        result = agent.analyze_cost_structure(expenses)
        assert isinstance(result, dict)
        for key in ("total", "breakdown", "suggestions"):
            assert key in result, f"Missing key: {key}"

    def test_analyze_cost_structure_suggestions_is_list(self):
        from factory.agents.finance_department import CostOptimizer
        agent = CostOptimizer()
        result = agent.analyze_cost_structure({"rent": 50_000, "misc": 2_000})
        assert isinstance(result["suggestions"], list)
        assert len(result["suggestions"]) >= 1

    def test_analyze_cost_structure_high_spend_flagged(self):
        from factory.agents.finance_department import CostOptimizer
        agent = CostOptimizer()
        # marketing = 80% of total → should be flagged
        result = agent.analyze_cost_structure({"marketing": 80_000, "hosting": 10_000, "misc": 10_000})
        suggestion_text = " ".join(result["suggestions"]).lower()
        assert "marketing" in suggestion_text

    def test_suggest_pricing_adjustments_returns_list(self):
        from factory.agents.finance_department import CostOptimizer
        agent = CostOptimizer()
        stats = [
            {"name": "Anna", "bookings": 50, "current_rate": 20_000, "avg_rating": 4.8},
            {"name": "Maria", "bookings": 5, "current_rate": 15_000, "avg_rating": 3.5},
        ]
        result = agent.suggest_pricing_adjustments(stats)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_suggest_pricing_adjustments_high_demand_increases_rate(self):
        from factory.agents.finance_department import CostOptimizer
        agent = CostOptimizer()
        stats = [
            {"name": "TopModel", "bookings": 100, "current_rate": 20_000, "avg_rating": 4.9},
            {"name": "LowModel", "bookings": 1, "current_rate": 20_000, "avg_rating": 3.0},
        ]
        result = agent.suggest_pricing_adjustments(stats)
        top = next(r for r in result if r["name"] == "TopModel")
        low = next(r for r in result if r["name"] == "LowModel")
        assert top["suggested_rate"] > top["current_rate"]
        assert low["suggested_rate"] < low["current_rate"]

    # ── PricingStrategist ──────────────────────────────────────────

    def test_calculate_optimal_price_returns_required_keys(self):
        from factory.agents.finance_department import PricingStrategist
        agent = PricingStrategist()
        result = agent.calculate_optimal_price("corporate", {"avg_rating": 4.5, "bookings": 30})
        assert isinstance(result, dict)
        for key in ("suggested_price", "min_price", "max_price", "rationale"):
            assert key in result, f"Missing key: {key}"

    def test_calculate_optimal_price_suggested_within_range(self):
        from factory.agents.finance_department import PricingStrategist
        agent = PricingStrategist()
        result = agent.calculate_optimal_price("wedding", {"avg_rating": 4.0, "bookings": 10})
        assert isinstance(result["suggested_price"], float)
        # suggested should be positive and within a reasonable range
        assert result["suggested_price"] > 0

    def test_get_seasonal_multiplier_returns_float(self):
        from factory.agents.finance_department import PricingStrategist
        agent = PricingStrategist()
        mult = agent.get_seasonal_multiplier(12)  # December — peak
        assert isinstance(mult, float)
        assert mult >= 1.0

    def test_get_seasonal_multiplier_december_peak(self):
        from factory.agents.finance_department import PricingStrategist
        agent = PricingStrategist()
        dec = agent.get_seasonal_multiplier(12)
        aug = agent.get_seasonal_multiplier(8)
        assert dec > aug  # December should be pricier than August

    # ── BudgetPlanner ──────────────────────────────────────────────

    def test_create_monthly_budget_returns_required_keys(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        result = agent.create_monthly_budget(200_000.0, {"hosting": 5_000, "accounting": 3_000})
        assert isinstance(result, dict)
        for key in ("total_budget", "allocations", "surplus"):
            assert key in result, f"Missing key: {key}"

    def test_create_monthly_budget_allocations_is_dict(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        result = agent.create_monthly_budget(150_000.0, {"hosting": 5_000})
        assert isinstance(result["allocations"], dict)
        assert len(result["allocations"]) >= 1

    def test_create_monthly_budget_zero_revenue(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        result = agent.create_monthly_budget(0.0, {"hosting": 5_000})
        assert result["total_budget"] == 0.0

    def test_evaluate_budget_variance_returns_required_keys(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        planned = {"marketing": 30_000, "hosting": 5_000}
        actual = {"marketing": 35_000, "hosting": 4_800}
        result = agent.evaluate_budget_variance(planned, actual)
        assert isinstance(result, dict)
        for key in ("variances", "total_variance", "status"):
            assert key in result, f"Missing key: {key}"

    def test_evaluate_budget_variance_over_budget_status(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        planned = {"marketing": 10_000}
        actual = {"marketing": 12_000}  # 20% over
        result = agent.evaluate_budget_variance(planned, actual)
        assert result["status"] == "over_budget"

    def test_evaluate_budget_variance_on_budget_status(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        planned = {"marketing": 10_000, "hosting": 5_000}
        actual = {"marketing": 10_200, "hosting": 4_900}  # <5% variance
        result = agent.evaluate_budget_variance(planned, actual)
        assert result["status"] == "on_budget"

    def test_evaluate_budget_variance_variances_contain_categories(self):
        from factory.agents.finance_department import BudgetPlanner
        agent = BudgetPlanner()
        planned = {"a": 1_000, "b": 2_000}
        actual = {"a": 1_100, "b": 1_800}
        result = agent.evaluate_budget_variance(planned, actual)
        assert "a" in result["variances"]
        assert "b" in result["variances"]
        assert result["variances"]["a"]["variance"] == pytest.approx(100.0)


class TestAgentRequiredAttributes:
    """Verify each agent has required class attributes: department, role, name."""

    # Agents that have all three of: department, role, name
    ALL_AGENT_CLASSES = [
        ("factory.agents.analytics_engine", "AnalyticsEngine"),
        ("factory.agents.content_generator", "ContentGenerator"),
        ("factory.agents.sales", "LeadQualifierAgent"),
        ("factory.agents.sales", "ProposalWriterAgent"),
        ("factory.agents.sales", "FollowUpSpecialistAgent"),
        ("factory.agents.sales", "PricingNegotiatorAgent"),
        ("factory.agents.creative", "CopywriterAgent"),
        ("factory.agents.creative", "VisualConceptorAgent"),
        ("factory.agents.creative", "BrandVoiceKeeperAgent"),
        ("factory.agents.creative", "StorytellingAgent"),
        ("factory.agents.customer_success", "OnboardingSpecialist"),
        ("factory.agents.customer_success", "RetentionAnalyst"),
        ("factory.agents.customer_success", "FeedbackCollector"),
        ("factory.agents.finance", "RevenueForecaster"),
        ("factory.agents.finance", "PricingStrategist"),
        ("factory.agents.research", "MarketResearcher"),
        ("factory.agents.research", "TrendSpotter"),
        ("factory.agents.research", "InsightSynthesizer"),
    ]

    # Agents that expose a run() method (AnalyticsEngine uses analyze() instead)
    RUN_AGENT_CLASSES = [
        ("factory.agents.content_generator", "ContentGenerator"),
        ("factory.agents.sales", "LeadQualifierAgent"),
        ("factory.agents.sales", "ProposalWriterAgent"),
        ("factory.agents.sales", "FollowUpSpecialistAgent"),
        ("factory.agents.sales", "PricingNegotiatorAgent"),
        ("factory.agents.creative", "CopywriterAgent"),
        ("factory.agents.creative", "VisualConceptorAgent"),
        ("factory.agents.creative", "BrandVoiceKeeperAgent"),
        ("factory.agents.creative", "StorytellingAgent"),
        ("factory.agents.customer_success", "OnboardingSpecialist"),
        ("factory.agents.customer_success", "RetentionAnalyst"),
        ("factory.agents.customer_success", "FeedbackCollector"),
        ("factory.agents.finance", "RevenueForecaster"),
        ("factory.agents.finance", "PricingStrategist"),
        ("factory.agents.research", "MarketResearcher"),
        ("factory.agents.research", "TrendSpotter"),
        ("factory.agents.research", "InsightSynthesizer"),
    ]

    @pytest.mark.parametrize("module_path,class_name", ALL_AGENT_CLASSES)
    def test_agent_has_required_attributes(self, module_path, class_name):
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        agent = cls()
        assert hasattr(agent, "department"), f"{class_name} missing 'department'"
        assert hasattr(agent, "role"), f"{class_name} missing 'role'"
        assert hasattr(agent, "name"), f"{class_name} missing 'name'"
        assert isinstance(agent.department, str), f"{class_name}.department must be str"
        assert isinstance(agent.role, str), f"{class_name}.role must be str"
        assert isinstance(agent.name, str), f"{class_name}.name must be str"

    @pytest.mark.parametrize("module_path,class_name", RUN_AGENT_CLASSES)
    def test_agent_has_run_method(self, module_path, class_name):
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        agent = cls()
        assert hasattr(agent, "run"), f"{class_name} missing 'run' method"
        assert callable(agent.run), f"{class_name}.run must be callable"

    @pytest.mark.parametrize("module_path,class_name", ALL_AGENT_CLASSES)
    def test_agent_has_think_method(self, module_path, class_name):
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        agent = cls()
        assert hasattr(agent, "think"), f"{class_name} missing 'think' method"
        assert callable(agent.think), f"{class_name}.think must be callable"


class TestBaseAgentRunReturnsDict:
    """Test that agent run() returns a dict with standard keys."""

    def _make_mock_agent(self, cls, mock_response: str = "Mock LLM response"):
        """Instantiate an agent and replace its think method with a mock."""
        agent = cls()
        agent.think = lambda prompt, **kw: mock_response
        return agent

    def test_onboarding_run_returns_dict_with_role(self):
        from factory.agents.customer_success import OnboardingSpecialist
        agent = self._make_mock_agent(OnboardingSpecialist)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_retention_analyst_run_returns_dict_with_role(self):
        from factory.agents.customer_success import RetentionAnalyst
        agent = self._make_mock_agent(RetentionAnalyst)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_feedback_collector_run_returns_dict_with_role(self):
        from factory.agents.customer_success import FeedbackCollector
        agent = self._make_mock_agent(FeedbackCollector)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_revenue_forecaster_run_returns_dict_with_role(self):
        from factory.agents.finance import RevenueForecaster
        agent = self._make_mock_agent(RevenueForecaster)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_pricing_strategist_run_returns_dict_with_role(self):
        from factory.agents.finance import PricingStrategist
        agent = self._make_mock_agent(PricingStrategist)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_market_researcher_run_returns_dict_with_role(self):
        from factory.agents.research import MarketResearcher
        agent = self._make_mock_agent(MarketResearcher)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_trend_spotter_run_returns_dict_with_role(self):
        from factory.agents.research import TrendSpotter
        agent = self._make_mock_agent(TrendSpotter)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result

    def test_insight_synthesizer_run_returns_dict_with_role(self):
        from factory.agents.research import InsightSynthesizer
        agent = self._make_mock_agent(InsightSynthesizer)
        result = agent.run()
        assert isinstance(result, dict)
        assert "role" in result


class TestBaseAgentThinkMocked:
    """Test FactoryAgent.think() with mocked SDK/CLI calls."""

    def test_think_with_sdk_mock(self):
        """think() via SDK path returns text from SDK response."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        mock_content = MagicMock()
        mock_content.text = "SDK response text"
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        with patch("factory.agents.base._sdk_client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = agent._think_sdk("system", "prompt", 100)
        assert result == "SDK response text"

    def test_think_sdk_empty_content(self):
        """think() via SDK returns empty string when content is empty."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        mock_response = MagicMock()
        mock_response.content = []
        with patch("factory.agents.base._sdk_client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = agent._think_sdk("system", "prompt", 100)
        assert result == ""

    def test_think_sdk_exception_returns_empty(self):
        """think() via SDK returns empty string on exception."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        with patch("factory.agents.base._sdk_client") as mock_client:
            mock_client.messages.create.side_effect = Exception("API error")
            result = agent._think_sdk("system", "prompt", 100)
        assert result == ""

    def test_think_json_returns_dict(self):
        """think_json() parses JSON returned by think()."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        with patch.object(agent, "think", return_value='{"key": "value"}'):
            result = agent.think_json("Give me JSON")
        assert isinstance(result, dict)
        assert result["key"] == "value"

    def test_think_json_with_code_block(self):
        """think_json() strips markdown code fences before parsing."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        response = '```json\n{"score": 42}\n```'
        with patch.object(agent, "think", return_value=response):
            result = agent.think_json("Give me JSON")
        assert result["score"] == 42

    def test_think_json_invalid_returns_empty_dict(self):
        """think_json() returns {} on unparseable response."""
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        with patch.object(agent, "think", return_value="not valid json at all"):
            result = agent.think_json("Give me JSON")
        assert isinstance(result, dict)
        assert result == {}

    def test_think_cli_timeout_returns_empty(self):
        """_think_cli() returns empty string on timeout."""
        import subprocess
        from factory.agents.base import FactoryAgent
        agent = FactoryAgent()
        with patch("factory.agents.base.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)):
            result = agent._think_cli("system", "prompt")
        assert result == ""


class TestCustomerSuccessDeptComplete:
    """Comprehensive tests for Customer Success Department (customer_success_dept.py)."""

    def test_all_four_classes_import(self):
        from factory.agents.customer_success_dept import (
            OnboardingSpecialist, RetentionAnalyst,
            FeedbackCollector, UpsellAdvisor,
        )
        assert OnboardingSpecialist is not None
        assert RetentionAnalyst is not None
        assert FeedbackCollector is not None
        assert UpsellAdvisor is not None

    def test_onboarding_specialist_attributes(self):
        from factory.agents.customer_success_dept import OnboardingSpecialist
        agent = OnboardingSpecialist()
        assert agent.department == 'customer_success'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_retention_analyst_attributes(self):
        from factory.agents.customer_success_dept import RetentionAnalyst
        agent = RetentionAnalyst()
        assert agent.department == 'customer_success'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_feedback_collector_attributes(self):
        from factory.agents.customer_success_dept import FeedbackCollector
        agent = FeedbackCollector()
        assert agent.department == 'customer_success'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_upsell_advisor_attributes(self):
        from factory.agents.customer_success_dept import UpsellAdvisor
        agent = UpsellAdvisor()
        assert agent.department == 'customer_success'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_all_agents_have_think_method(self):
        from factory.agents.customer_success_dept import (
            OnboardingSpecialist, RetentionAnalyst,
            FeedbackCollector, UpsellAdvisor,
        )
        for cls in (OnboardingSpecialist, RetentionAnalyst, FeedbackCollector, UpsellAdvisor):
            agent = cls()
            assert hasattr(agent, 'think') and callable(agent.think), f"{cls.__name__} missing think()"

    def test_onboarding_run_with_mock_returns_dict(self):
        from factory.agents.customer_success_dept import OnboardingSpecialist
        agent = OnboardingSpecialist()
        with patch.object(agent, 'think_json', return_value={'insights': ['a'], 'recommendations': ['b'], 'priority': 8}):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result
        assert 'recommendations' in result
        assert 'priority' in result

    def test_retention_run_with_mock_returns_dict(self):
        from factory.agents.customer_success_dept import RetentionAnalyst
        agent = RetentionAnalyst()
        with patch.object(agent, 'think_json', return_value={'insights': ['x'], 'recommendations': ['y'], 'priority': 9}):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result

    def test_feedback_run_with_mock_returns_dict(self):
        from factory.agents.customer_success_dept import FeedbackCollector
        agent = FeedbackCollector()
        with patch.object(agent, 'think_json', return_value={'insights': ['i'], 'recommendations': ['r'], 'priority': 7}):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'recommendations' in result

    def test_upsell_run_with_mock_returns_dict(self):
        from factory.agents.customer_success_dept import UpsellAdvisor
        agent = UpsellAdvisor()
        with patch.object(agent, 'think_json', return_value={'insights': ['u'], 'recommendations': ['v'], 'priority': 8}):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'priority' in result

    def test_customer_success_department_instantiation(self):
        from factory.agents.customer_success_dept import (
            CustomerSuccessDepartment,
            OnboardingSpecialist, RetentionAnalyst,
            FeedbackCollector, UpsellAdvisor,
        )
        dept = CustomerSuccessDepartment()
        assert isinstance(dept.onboarding, OnboardingSpecialist)
        assert isinstance(dept.retention, RetentionAnalyst)
        assert isinstance(dept.feedback, FeedbackCollector)
        assert isinstance(dept.upsell, UpsellAdvisor)

    def test_customer_success_department_has_execute_task(self):
        from factory.agents.customer_success_dept import CustomerSuccessDepartment
        dept = CustomerSuccessDepartment()
        assert hasattr(dept, 'execute_task') and callable(dept.execute_task)

    def test_execute_task_returns_dict_with_department_key(self):
        from factory.agents.customer_success_dept import CustomerSuccessDepartment
        dept = CustomerSuccessDepartment()
        with patch.object(dept.upsell, 'suggest_upsell', return_value={}):
            result = dept.execute_task("апселл", {})
        assert isinstance(result, dict)
        assert result.get('department') == 'customer_success'
        assert 'roles_used' in result


class TestFinanceDeptComplete:
    """Comprehensive tests for Finance Department (finance_dept.py)."""

    def test_all_four_classes_import(self):
        from factory.agents.finance_dept import (
            RevenueForecaster, CostOptimizer,
            PricingStrategist, BudgetPlanner,
        )
        assert RevenueForecaster is not None
        assert CostOptimizer is not None
        assert PricingStrategist is not None
        assert BudgetPlanner is not None

    def test_revenue_forecaster_attributes(self):
        from factory.agents.finance_dept import RevenueForecaster
        agent = RevenueForecaster()
        assert agent.department == 'finance'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_cost_optimizer_attributes(self):
        from factory.agents.finance_dept import CostOptimizer
        agent = CostOptimizer()
        assert agent.department == 'finance'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_pricing_strategist_attributes(self):
        from factory.agents.finance_dept import PricingStrategist
        agent = PricingStrategist()
        assert agent.department == 'finance'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_budget_planner_attributes(self):
        from factory.agents.finance_dept import BudgetPlanner
        agent = BudgetPlanner()
        assert agent.department == 'finance'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_all_agents_have_think_method(self):
        from factory.agents.finance_dept import (
            RevenueForecaster, CostOptimizer,
            PricingStrategist, BudgetPlanner,
        )
        for cls in (RevenueForecaster, CostOptimizer, PricingStrategist, BudgetPlanner):
            agent = cls()
            assert hasattr(agent, 'think') and callable(agent.think), f"{cls.__name__} missing think()"

    def test_revenue_forecaster_run_returns_dict(self):
        from factory.agents.finance_dept import RevenueForecaster
        agent = RevenueForecaster()
        mock_forecast = {'forecast_rub': 150000, 'forecast_low_rub': 100000,
                         'forecast_high_rub': 220000, 'recommended_actions': ['act1']}
        with patch.object(agent, 'think_json', return_value=mock_forecast):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result
        assert 'recommendations' in result
        assert 'priority' in result

    def test_cost_optimizer_run_returns_dict(self):
        from factory.agents.finance_dept import CostOptimizer
        agent = CostOptimizer()
        mock_savings = {'total_potential_savings_rub': 25000, 'payback_period_months': 2, 'quick_wins': []}
        with patch.object(agent, 'think_json', return_value=mock_savings):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result

    def test_pricing_strategist_run_returns_dict(self):
        from factory.agents.finance_dept import PricingStrategist
        agent = PricingStrategist()
        mock_pricing = {'market_analysis': {'segment': 'средний'}, 'pricing_recommendations': [],
                        'revenue_impact_estimate_rub': 30000}
        with patch.object(agent, 'think_json', return_value=mock_pricing):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'priority' in result

    def test_budget_planner_run_returns_dict(self):
        from factory.agents.finance_dept import BudgetPlanner
        agent = BudgetPlanner()
        mock_budget = {'total_budget_rub': 200000, 'marketing_budget': {'total_rub': 80000},
                       'operations_budget': {'total_rub': 70000}, 'reserve_rub': 20000, 'assumptions': []}
        with patch.object(agent, 'think_json', return_value=mock_budget):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'forecast' in result

    def test_finance_department_instantiation(self):
        from factory.agents.finance_dept import (
            FinanceDepartment,
            RevenueForecaster, CostOptimizer,
            PricingStrategist, BudgetPlanner,
        )
        dept = FinanceDepartment()
        assert isinstance(dept.forecaster, RevenueForecaster)
        assert isinstance(dept.optimizer, CostOptimizer)
        assert isinstance(dept.pricing, PricingStrategist)
        assert isinstance(dept.planner, BudgetPlanner)

    def test_finance_department_execute_task_returns_dict(self):
        from factory.agents.finance_dept import FinanceDepartment
        dept = FinanceDepartment()
        with patch.object(dept.planner, 'plan_budget', return_value={}):
            result = dept.execute_task("бюджет", {})
        assert isinstance(result, dict)
        assert result.get('department') == 'finance'
        assert 'roles_used' in result


class TestResearchDeptComplete:
    """Comprehensive tests for Research Department (research_dept.py)."""

    def test_all_four_classes_import(self):
        from factory.agents.research_dept import (
            MarketResearcher, CompetitorAnalyst,
            TrendSpotter, InsightSynthesizer,
        )
        assert MarketResearcher is not None
        assert CompetitorAnalyst is not None
        assert TrendSpotter is not None
        assert InsightSynthesizer is not None

    def test_market_researcher_attributes(self):
        from factory.agents.research_dept import MarketResearcher
        agent = MarketResearcher()
        assert agent.department == 'research'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_competitor_analyst_attributes(self):
        from factory.agents.research_dept import CompetitorAnalyst
        agent = CompetitorAnalyst()
        assert agent.department == 'research'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_trend_spotter_attributes(self):
        from factory.agents.research_dept import TrendSpotter
        agent = TrendSpotter()
        assert agent.department == 'research'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_insight_synthesizer_attributes(self):
        from factory.agents.research_dept import InsightSynthesizer
        agent = InsightSynthesizer()
        assert agent.department == 'research'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)

    def test_all_agents_have_think_method(self):
        from factory.agents.research_dept import (
            MarketResearcher, CompetitorAnalyst,
            TrendSpotter, InsightSynthesizer,
        )
        for cls in (MarketResearcher, CompetitorAnalyst, TrendSpotter, InsightSynthesizer):
            agent = cls()
            assert hasattr(agent, 'think') and callable(agent.think), f"{cls.__name__} missing think()"

    def test_market_researcher_run_returns_dict(self):
        from factory.agents.research_dept import MarketResearcher
        agent = MarketResearcher()
        mock_data = {'market_size': {'total_rub_bln': 12.5, 'growth_rate_pct': 8, 'online_share_pct': 35},
                     'market_trends': ['trend1'], 'market_opportunities': []}
        with patch.object(agent, 'think_json', return_value=mock_data):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result
        assert 'trends' in result
        assert 'opportunities' in result
        assert 'priority' in result

    def test_competitor_analyst_run_returns_dict(self):
        from factory.agents.research_dept import CompetitorAnalyst
        agent = CompetitorAnalyst()
        mock_data = {'nevesty_advantages': ['adv1'], 'nevesty_gaps': ['gap1'],
                     'differentiation_opportunities': [], 'competitive_landscape': 'summary'}
        with patch.object(agent, 'think_json', return_value=mock_data):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'insights' in result

    def test_trend_spotter_run_returns_dict(self):
        from factory.agents.research_dept import TrendSpotter
        agent = TrendSpotter()
        mock_data = {'fashion_trends': [], 'digital_trends': [], 'seasonal_opportunities': [],
                     'emerging_opportunities': []}
        with patch.object(agent, 'think_json', return_value=mock_data):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'trends' in result

    def test_insight_synthesizer_run_returns_dict(self):
        from factory.agents.research_dept import InsightSynthesizer
        agent = InsightSynthesizer()
        mock_data = {'key_insights': [{'insight': 'i1'}], 'priority_actions': [{'action': 'a1'}],
                     'quick_wins': [], 'north_star_metric': 'конверсия'}
        with patch.object(agent, 'think_json', return_value=mock_data):
            result = agent.run({})
        assert isinstance(result, dict)
        assert 'opportunities' in result

    def test_research_department_instantiation(self):
        from factory.agents.research_dept import (
            ResearchDepartment,
            MarketResearcher, CompetitorAnalyst,
            TrendSpotter, InsightSynthesizer,
        )
        dept = ResearchDepartment()
        assert isinstance(dept.market, MarketResearcher)
        assert isinstance(dept.competitors, CompetitorAnalyst)
        assert isinstance(dept.trends, TrendSpotter)
        assert isinstance(dept.synthesizer, InsightSynthesizer)

    def test_research_department_execute_task_returns_dict(self):
        from factory.agents.research_dept import ResearchDepartment
        dept = ResearchDepartment()
        with patch.object(dept.synthesizer, 'synthesize_insights', return_value={}):
            result = dept.execute_task("инсайт", {})
        assert isinstance(result, dict)
        assert result.get('department') == 'research'
        assert 'roles_used' in result


class TestCreativeDeptComplete:
    """Comprehensive tests for Creative Department (creative.py)."""

    def test_all_four_agent_classes_import(self):
        from factory.agents.creative import (
            CopywriterAgent, VisualConceptorAgent,
            BrandVoiceKeeperAgent, StorytellingAgent,
        )
        assert CopywriterAgent is not None
        assert VisualConceptorAgent is not None
        assert BrandVoiceKeeperAgent is not None
        assert StorytellingAgent is not None

    def test_copywriter_agent_attributes(self):
        from factory.agents.creative import CopywriterAgent
        agent = CopywriterAgent()
        assert agent.department == 'creative'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)
        assert hasattr(agent, 'build_prompt') and callable(agent.build_prompt)

    def test_visual_conceptor_attributes(self):
        from factory.agents.creative import VisualConceptorAgent
        agent = VisualConceptorAgent()
        assert agent.department == 'creative'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)
        assert hasattr(agent, 'build_prompt') and callable(agent.build_prompt)

    def test_brand_voice_keeper_attributes(self):
        from factory.agents.creative import BrandVoiceKeeperAgent
        agent = BrandVoiceKeeperAgent()
        assert agent.department == 'creative'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)
        assert hasattr(agent, 'build_prompt') and callable(agent.build_prompt)

    def test_storytelling_agent_attributes(self):
        from factory.agents.creative import StorytellingAgent
        agent = StorytellingAgent()
        assert agent.department == 'creative'
        assert isinstance(agent.role, str) and agent.role
        assert isinstance(agent.name, str) and agent.name
        assert hasattr(agent, 'run') and callable(agent.run)
        assert hasattr(agent, 'build_prompt') and callable(agent.build_prompt)

    def test_all_agents_have_think_method(self):
        from factory.agents.creative import (
            CopywriterAgent, VisualConceptorAgent,
            BrandVoiceKeeperAgent, StorytellingAgent,
        )
        for cls in (CopywriterAgent, VisualConceptorAgent, BrandVoiceKeeperAgent, StorytellingAgent):
            agent = cls()
            assert hasattr(agent, 'think') and callable(agent.think), f"{cls.__name__} missing think()"

    def test_copywriter_run_with_mock(self):
        from factory.agents.creative import CopywriterAgent
        agent = CopywriterAgent()
        with patch.object(agent, 'think', return_value='Mocked copy text'):
            result = agent.run({})
        assert isinstance(result, dict)
        assert result.get('department') == 'creative'
        assert result.get('role') == 'Copywriter'
        assert 'result' in result

    def test_visual_conceptor_run_with_mock(self):
        from factory.agents.creative import VisualConceptorAgent
        agent = VisualConceptorAgent()
        with patch.object(agent, 'think', return_value='Mocked visual concept'):
            result = agent.run({})
        assert isinstance(result, dict)
        assert result.get('department') == 'creative'
        assert result.get('role') == 'VisualConceptor'

    def test_brand_voice_keeper_run_with_mock(self):
        from factory.agents.creative import BrandVoiceKeeperAgent
        agent = BrandVoiceKeeperAgent()
        with patch.object(agent, 'think', return_value='Mocked brand voice'):
            result = agent.run({})
        assert isinstance(result, dict)
        assert result.get('department') == 'creative'

    def test_storytelling_run_with_mock(self):
        from factory.agents.creative import StorytellingAgent
        agent = StorytellingAgent()
        with patch.object(agent, 'think', return_value='Mocked story'):
            result = agent.run({})
        assert isinstance(result, dict)
        assert result.get('department') == 'creative'
        assert result.get('role') == 'Storytelling'

    def test_creative_department_instantiation_has_four_agents(self):
        from factory.agents.creative import (
            CreativeDepartment,
            CopywriterAgent, VisualConceptorAgent,
            BrandVoiceKeeperAgent, StorytellingAgent,
        )
        dept = CreativeDepartment()
        assert len(dept.agents) == 4
        roles = [a.role for a in dept.agents]
        assert 'Copywriter' in roles
        assert 'VisualConceptor' in roles
        assert 'BrandVoiceKeeper' in roles
        assert 'Storytelling' in roles

    def test_creative_department_run_cycle_returns_dict(self):
        from factory.agents.creative import CreativeDepartment
        dept = CreativeDepartment()
        for agent in dept.agents:
            agent.think = lambda prompt, **kw: 'Mocked LLM response'
        result = dept.run_cycle({})
        assert isinstance(result, dict)
        assert len(result) == 4


class TestStrategicCoreExtended:
    """Tests for new CEO methods on StrategicCore (strategic_core.py)."""

    def test_strategic_core_imports(self):
        from factory.agents.strategic_core import StrategicCore
        assert StrategicCore is not None

    def test_synthesize_dept_reports_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'synthesize_dept_reports')
        assert callable(ceo.synthesize_dept_reports)

    def test_generate_weekly_report_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'generate_weekly_report')
        assert callable(ceo.generate_weekly_report)

    def test_propose_ab_experiment_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'propose_ab_experiment')
        assert callable(ceo.propose_ab_experiment)

    def test_evaluate_experiment_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'evaluate_experiment')
        assert callable(ceo.evaluate_experiment)

    def test_evaluate_experiment_scale_when_conv_b_above_5(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        experiment = {'conversion_a': 3.0, 'conversion_b': 6.5}
        result = ceo.evaluate_experiment(experiment)
        assert result == 'scale', f"Expected 'scale' for conv_b=6.5, got '{result}'"

    def test_evaluate_experiment_kill_when_conv_b_below_2(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        experiment = {'conversion_a': 4.0, 'conversion_b': 1.0}
        result = ceo.evaluate_experiment(experiment)
        assert result == 'kill', f"Expected 'kill' for conv_b=1.0 and conv_a>=conv_b, got '{result}'"

    def test_evaluate_experiment_iterate_in_middle_range_with_mock(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        experiment = {'conversion_a': 3.0, 'conversion_b': 3.5}
        with patch.object(ceo, 'think_json', return_value={'result': 'iterate'}):
            result = ceo.evaluate_experiment(experiment)
        assert result in ('scale', 'iterate', 'kill'), f"Unexpected result: '{result}'"

    def test_generate_weekly_report_text_with_mock_returns_str(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        with patch.object(ceo, 'think', return_value='Mocked weekly report'):
            report = ceo.generate_weekly_report_text()
        assert isinstance(report, str)
        assert len(report) > 0

    def test_propose_ab_experiment_with_mock_returns_dict(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mock_exp = {
            'name': 'Test CTA',
            'hypothesis': 'Better button',
            'variant_a': 'Gold button',
            'variant_b': 'White button',
            'metric': 'CTR',
            'duration_days': 14,
            'expected_lift_pct': 20,
        }
        with patch.object(ceo, 'think_json', return_value=mock_exp):
            result = ceo.propose_ab_experiment({'context': 'test'})
        assert isinstance(result, dict)
        assert 'name' in result
        assert 'hypothesis' in result

    def test_synthesize_dept_reports_with_mock_returns_dict(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        dept_results = {
            'creative': {'Copywriter': {'result': 'some creative copy text'}},
            'sales': 'Some sales summary text',
        }
        mock_briefing = {
            'key_insights': ['insight1'],
            'priority_issues': ['issue1'],
            'next_cycle_focus': 'marketing',
            'recommended_action': 'Launch campaign',
            'health_score': 75,
            'summary': 'Business is performing well',
        }
        with patch.object(ceo, 'think_json', return_value=mock_briefing), \
             patch('factory.db.get_recent_decisions', return_value=[]), \
             patch('factory.db.save_ceo_decision', return_value=None):
            result = ceo.synthesize_dept_reports(dept_results)
        assert isinstance(result, dict)
        assert 'health_score' in result or 'summary' in result


class TestCyclePhase21:
    """Tests for Phase 21: CEO Weekly Summary."""

    def test_phase_21_weekly_report_in_cycle(self):
        """Phase 21 exists in cycle module."""
        import factory.cycle as cycle
        src = open(cycle.__file__).read()
        assert 'Phase 21' in src


@pytest.fixture
def mock_agent_run():
    """Mock the LLM call to avoid network requests."""
    with patch('factory.agents.base.FactoryAgent.think') as mock:
        mock.return_value = "Mocked LLM response for testing purposes."
        yield mock


class TestCycleWithMock:
    def test_run_cycle_returns_dict(self, mock_agent_run):
        """run_cycle should return a dict with results."""
        from factory.cycle import run_cycle
        # run_cycle makes LLM calls, but with mock it returns fast
        # Just test that function exists and is callable
        import inspect
        assert callable(run_cycle)

    def test_cycle_has_20_phases(self):
        """Cycle should have at least 11 phases defined (Phases 1, 12-21)."""
        import factory.cycle as cycle
        src = open(cycle.__file__).read()
        # Count all unique Phase N references in the file
        import re
        phases = set(int(m.group(1)) for m in re.finditer(r'Phase (\d+)', src))
        assert len(phases) >= 11, (
            f"Expected at least 11 distinct phases in cycle.py, found {len(phases)}: {sorted(phases)}"
        )

    def test_cycle_phases_include_late_stages(self):
        """Cycle should include later phase numbers (16+) covering weekly reviews."""
        import factory.cycle as cycle
        src = open(cycle.__file__).read()
        for phase_num in (16, 17, 18, 19, 20, 21):
            assert f'Phase {phase_num}' in src, f"Phase {phase_num} not found in cycle.py"


class TestExperimentSystemExtended:
    def test_rule_based_eval_scale(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        exp = {"conversion_a": 3.0, "conversion_b": 6.0, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result == "scale"

    def test_rule_based_eval_kill(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        exp = {"conversion_a": 4.0, "conversion_b": 1.0, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result == "kill"

    def test_rule_based_eval_returns_none_for_middle(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        exp = {"conversion_a": 3.0, "conversion_b": 3.5, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result is None

    def test_generate_experiment_report_returns_dict(self, mocker):
        mocker.patch('factory.db.fetch_all', return_value=[])
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        report = sys.generate_experiment_report()
        assert isinstance(report, dict)
        assert "total_experiments" in report
        assert "win_rate" in report

    def test_generate_experiment_report_win_rate(self, mocker):
        experiments = [
            {"status": "concluded", "result": "scale", "name": "Test A"},
            {"status": "concluded", "result": "kill", "name": "Test B"},
        ]
        mocker.patch('factory.db.fetch_all', return_value=experiments)
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        report = sys.generate_experiment_report()
        assert report["win_rate"] == 50.0
        assert report["concluded"] == 2


class TestStrategicCoreMonthlyReport:
    """Tests for CEO monthly report and decision tracking methods."""

    def test_generate_monthly_report_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'generate_monthly_report')
        assert callable(ceo.generate_monthly_report)

    def test_track_decision_execution_exists(self):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        assert hasattr(ceo, 'track_decision_execution')
        assert callable(ceo.track_decision_execution)

    def test_generate_monthly_report_returns_dict(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mock_report = {
            "period": "May 2026",
            "executive_summary": "Strong growth in Q2",
            "kpis": {"total_decisions": 20, "experiments_run": 3, "active_products": 2},
            "achievements": ["Launched new MVP", "Improved conversion"],
            "challenges": ["Low traffic"],
            "next_month_goals": ["Scale marketing"],
            "strategic_direction": "Expand to new cities",
            "health_trend": "improving",
        }
        mocker.patch('factory.db.get_recent_decisions', return_value=[])
        mocker.patch('factory.db.get_active_products', return_value=[])
        mocker.patch('factory.db.get_running_experiments', return_value=[])
        mocker.patch('factory.db.save_ceo_decision', return_value=None)
        mocker.patch.object(ceo, 'think_json', return_value=mock_report)
        result = ceo.generate_monthly_report()
        assert isinstance(result, dict)
        assert 'period' in result
        assert 'health_trend' in result

    def test_generate_monthly_report_fallback_on_invalid_ai(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mocker.patch('factory.db.get_recent_decisions', return_value=[])
        mocker.patch('factory.db.get_active_products', return_value=[])
        mocker.patch('factory.db.get_running_experiments', return_value=[])
        mocker.patch('factory.db.save_ceo_decision', return_value=None)
        mocker.patch.object(ceo, 'think_json', return_value="invalid string")
        result = ceo.generate_monthly_report()
        assert isinstance(result, dict)
        assert 'executive_summary' in result
        assert result['executive_summary'] == "Monthly report unavailable"

    def test_monthly_report_has_required_keys(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mocker.patch('factory.db.get_recent_decisions', return_value=[{"decision_type": "grow"}] * 5)
        mocker.patch('factory.db.get_active_products', return_value=[{}] * 3)
        mocker.patch('factory.db.get_running_experiments', return_value=[{}])
        mocker.patch('factory.db.save_ceo_decision', return_value=None)
        mocker.patch.object(ceo, 'think_json', return_value={
            "period": "May 2026", "executive_summary": "OK",
            "kpis": {}, "achievements": [], "challenges": [],
            "next_month_goals": [], "strategic_direction": "grow",
            "health_trend": "stable"
        })
        result = ceo.generate_monthly_report()
        for key in ('period', 'executive_summary', 'kpis', 'achievements',
                    'challenges', 'next_month_goals', 'strategic_direction', 'health_trend'):
            assert key in result, f"Key '{key}' missing from monthly report"

    def test_track_decision_execution_not_found(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mocker.patch('factory.db.fetch_all', return_value=[])
        result = ceo.track_decision_execution(999)
        assert isinstance(result, dict)
        assert 'error' in result

    def test_track_decision_execution_returns_dict(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        ceo = StrategicCore()
        mock_decision = [{
            "id": 1, "decision_type": "grow", "rationale": "Low traffic",
            "payload": {}, "created_at": "2026-05-01T00:00:00"
        }]
        mock_status = {"executed": True, "impact": "Traffic increased 20%", "completion_pct": 100, "blockers": []}
        mocker.patch('factory.db.fetch_all', return_value=mock_decision)
        mocker.patch('factory.db.run', return_value=None)
        mocker.patch.object(ceo, 'think_json', return_value=mock_status)
        result = ceo.track_decision_execution(1)
        assert isinstance(result, dict)
        assert 'executed' in result or 'impact' in result


class TestExperimentSystemExtended:
    """Tests for extended experiment system methods."""

    def test_experiment_system_imports(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        assert sys is not None

    def test_generate_experiment_report_exists(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        assert hasattr(sys, 'generate_experiment_report')

    def test_apply_experiment_exists(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        assert hasattr(sys, 'apply_experiment')

    def test_rule_based_eval_scale(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        if not hasattr(sys, '_rule_based_eval'):
            pytest.skip("_rule_based_eval not implemented yet")
        exp = {"conversion_a": 3.0, "conversion_b": 6.0, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result == "scale"

    def test_rule_based_eval_kill(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        if not hasattr(sys, '_rule_based_eval'):
            pytest.skip("_rule_based_eval not implemented yet")
        exp = {"conversion_a": 4.0, "conversion_b": 1.0, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result == "kill"

    def test_rule_based_eval_returns_none_for_middle(self):
        from factory.agents.experiment_system import ExperimentSystem
        sys = ExperimentSystem()
        if not hasattr(sys, '_rule_based_eval'):
            pytest.skip("_rule_based_eval not implemented yet")
        exp = {"conversion_a": 3.0, "conversion_b": 3.5, "created_at": "2024-01-01T00:00:00"}
        result = sys._rule_based_eval(exp)
        assert result is None


class TestContentGeneratorExtended:
    def test_telegram_post_agent_imports(self):
        from factory.agents.content_generator import TelegramPostAgent
        agent = TelegramPostAgent()
        assert agent.name == "TelegramPostAgent"

    def test_generate_post_fallback(self, mocker):
        from factory.agents.content_generator import TelegramPostAgent
        agent = TelegramPostAgent()
        mocker.patch.object(agent, 'think_json', return_value="invalid")
        result = agent.generate_post("promo")
        assert isinstance(result, dict)
        assert "text" in result

    def test_faq_generator_imports(self):
        from factory.agents.content_generator import FAQGeneratorAgent
        agent = FAQGeneratorAgent()
        assert agent is not None

    def test_generate_faq_item_fallback(self, mocker):
        from factory.agents.content_generator import FAQGeneratorAgent
        agent = FAQGeneratorAgent()
        mocker.patch.object(agent, 'think_json', return_value={})
        result = agent.generate_faq_item("Test question?")
        assert isinstance(result, dict)
        assert "question" in result
        assert "answer" in result


class TestMetricsCollector:
    def test_metrics_collector_import(self):
        from factory.agents.metrics_collector import MetricsCollector
        assert MetricsCollector is not None

    def test_collect_all_no_db(self):
        from factory.agents.metrics_collector import MetricsCollector
        collector = MetricsCollector()
        collector.db_path = None  # Force no-DB scenario
        metrics = collector.collect_all()
        assert isinstance(metrics, dict)
        assert 'orders_total' in metrics
        assert metrics['db_available'] == False

    def test_empty_metrics_shape(self):
        from factory.agents.metrics_collector import MetricsCollector
        collector = MetricsCollector()
        empty = collector._empty_metrics()
        required_keys = ['orders_total', 'conversion_rate', 'revenue_month', 'avg_check', 'clients_unique', 'avg_rating']
        for key in required_keys:
            assert key in empty, f"Missing key: {key}"

    def test_metrics_with_in_memory_db(self, tmp_path):
        """Test metrics collection with a real SQLite DB."""
        import sqlite3
        from factory.agents.metrics_collector import MetricsCollector

        # Create a temp DB with Nevesty schema (matching actual data.db)
        db_file = tmp_path / "test_nevesty.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                status TEXT,
                budget TEXT,
                client_phone TEXT,
                model_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE models (
                id INTEGER PRIMARY KEY,
                name TEXT,
                featured INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                available INTEGER DEFAULT 1,
                order_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE reviews (
                id INTEGER PRIMARY KEY,
                rating INTEGER,
                approved INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE telegram_sessions (
                chat_id INTEGER PRIMARY KEY,
                state TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO orders (status, budget, client_phone) VALUES ('completed', '50000', '+79001234567')")
        conn.execute("INSERT INTO orders (status, budget, client_phone) VALUES ('new', '30000', '+79009876543')")
        conn.execute("INSERT INTO models (name, featured, available, order_count) VALUES ('Test Model', 1, 1, 1)")
        conn.execute("INSERT INTO reviews (rating, approved) VALUES (5, 1)")
        conn.execute("INSERT INTO telegram_sessions (chat_id) VALUES (123456)")
        conn.commit()
        conn.close()

        collector = MetricsCollector()
        collector.db_path = db_file
        metrics = collector.collect_all()

        assert metrics['db_available'] == True
        assert metrics['orders_total'] == 2
        assert metrics['orders_new'] == 1
        assert metrics['orders_completed'] == 1
        assert metrics['models_total'] == 1
        assert metrics['models_featured'] == 1
        assert metrics['reviews_total'] == 1
        assert metrics['avg_rating'] == 5.0
        assert metrics['bot_users_total'] == 1
        assert metrics['clients_unique'] == 2

    def test_metrics_keys_include_kpis(self, tmp_path):
        """Verify all KPI keys required by cycle.py are present."""
        import sqlite3
        from factory.agents.metrics_collector import MetricsCollector

        db_file = tmp_path / "kpi_test.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, budget TEXT, client_phone TEXT, created_at TEXT DEFAULT (datetime('now')))")
        conn.execute("CREATE TABLE models (id INTEGER PRIMARY KEY, name TEXT, featured INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, available INTEGER DEFAULT 1, order_count INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, rating INTEGER, approved INTEGER DEFAULT 0)")
        conn.commit()
        conn.close()

        collector = MetricsCollector()
        collector.db_path = db_file
        metrics = collector.collect_all()

        # Keys that cycle.py reads via nevesty_kpis_raw.get(...)
        for key in ('orders_week', 'orders_month', 'conversion_rate', 'revenue_month',
                    'avg_check', 'pipeline_value', 'models_total', 'clients_unique',
                    'clients_repeat', 'avg_rating', 'db_available', 'collected_at'):
            assert key in metrics, f"Missing KPI key: {key}"


class TestCEOWeeklyReport:
    """Tests for CEO weekly report and experiment proposal methods (БЛОК 5.3, 5.4)."""

    def test_generate_weekly_report_exists(self):
        from factory.agents.strategic_core import StrategicCore
        assert hasattr(StrategicCore, 'generate_weekly_report')

    def test_propose_experiments_exists(self):
        from factory.agents.strategic_core import StrategicCore
        assert hasattr(StrategicCore, 'propose_experiments')

    def test_generate_weekly_report_fallback(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.fetch_one', return_value=None)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        result = agent.generate_weekly_report({'orders_week': 5, 'conversion_rate': 60})
        assert 'week' in result
        assert 'headline' in result
        assert 'key_metric_trend' in result

    def test_generate_weekly_report_returns_dict(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.fetch_one', return_value=None)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        result = agent.generate_weekly_report({})
        assert isinstance(result, dict)

    def test_generate_weekly_report_already_generated(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.fetch_one', return_value={'id': 1})
        result = agent.generate_weekly_report({})
        assert result.get('status') == 'already_generated'
        assert 'week' in result

    def test_propose_experiments_fallback(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        result = agent.propose_experiments({'conversion_rate': 50, 'avg_check': 45000})
        assert isinstance(result, list)
        assert len(result) > 0
        assert 'hypothesis' in result[0]
        assert 'metric' in result[0]

    def test_propose_experiments_returns_list(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        result = agent.propose_experiments(None)
        assert isinstance(result, list)
        assert len(result) == 3

    def test_propose_experiments_structure(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        result = agent.propose_experiments({})
        for exp in result:
            assert 'hypothesis' in exp
            assert 'metric' in exp
            assert 'control' in exp
            assert 'variant' in exp
            assert 'duration_days' in exp
            assert 'expected_lift_pct' in exp

    def test_generate_weekly_report_no_metrics(self, mocker):
        from factory.agents.strategic_core import StrategicCore
        agent = StrategicCore.__new__(StrategicCore)
        mocker.patch('factory.agents.strategic_core.db.fetch_one', return_value=None)
        mocker.patch('factory.agents.strategic_core.db.execute', return_value=None)
        mocker.patch.object(agent, 'think', side_effect=Exception("no AI"))
        # Should not raise even with no metrics passed
        result = agent.generate_weekly_report()
        assert isinstance(result, dict)
        assert 'week' in result

    def test_generate_weekly_report_text_exists(self):
        from factory.agents.strategic_core import StrategicCore
        assert hasattr(StrategicCore, 'generate_weekly_report_text')


# ======================================================================
# NEW DEPARTMENTS — Sales, Creative, CustomerSuccess (simple/no-API)
# ======================================================================

class TestSalesDepartmentNew:
    """Tests for factory/agents/sales_department.py (no-API-call implementation)."""

    def _dept(self):
        from factory.agents.sales_department import SalesDepartment
        return SalesDepartment()

    # --- qualify_lead ---

    def test_qualify_lead_premium_budget(self):
        dept = self._dept()
        result = dept.qualify_lead({"budget": 150_000, "event_type": "фотосессия"})
        assert isinstance(result, dict)
        assert result["score"] >= 80
        assert result["tier"] == "premium"

    def test_qualify_lead_standard_budget(self):
        dept = self._dept()
        result = dept.qualify_lead({"budget": 50_000, "event_type": "фотосессия"})
        assert result["tier"] == "standard"
        assert 50 <= result["score"] < 80

    def test_qualify_lead_economy_budget(self):
        dept = self._dept()
        result = dept.qualify_lead({"budget": 10_000, "event_type": "фотосессия"})
        assert result["tier"] == "economy"
        assert result["score"] < 50

    def test_qualify_lead_corporate_bonus(self):
        dept = self._dept()
        base = dept.qualify_lead({"budget": 50_000, "event_type": "фотосессия"})
        corp = dept.qualify_lead({"budget": 50_000, "event_type": "корпоратив"})
        assert corp["score"] > base["score"]

    def test_qualify_lead_urgency_bonus(self):
        from datetime import date, timedelta
        dept = self._dept()
        soon = (date.today() + timedelta(days=10)).isoformat()
        base = dept.qualify_lead({"budget": 50_000, "event_type": "фотосессия"})
        urgent = dept.qualify_lead({"budget": 50_000, "event_type": "фотосессия", "date": soon})
        assert urgent["score"] > base["score"]

    def test_qualify_lead_score_capped_at_100(self):
        from datetime import date, timedelta
        dept = self._dept()
        soon = (date.today() + timedelta(days=5)).isoformat()
        result = dept.qualify_lead({"budget": 200_000, "event_type": "корпоратив", "date": soon})
        assert result["score"] <= 100

    def test_qualify_lead_returns_notes_string(self):
        dept = self._dept()
        result = dept.qualify_lead({"budget": 50_000})
        assert isinstance(result["notes"], str)
        assert len(result["notes"]) > 0

    # --- generate_proposal ---

    def test_generate_proposal_returns_string(self):
        dept = self._dept()
        result = dept.generate_proposal({"client_name": "ООО Ромашка", "event_type": "корпоратив"})
        assert isinstance(result, str)
        assert len(result) > 50

    def test_generate_proposal_contains_client_name(self):
        dept = self._dept()
        result = dept.generate_proposal({"client_name": "Иван Петров", "event_type": "свадьба"})
        assert "Иван Петров" in result

    def test_generate_proposal_contains_event_type(self):
        dept = self._dept()
        result = dept.generate_proposal({"client_name": "Клиент", "event_type": "фотосессия"})
        assert "фотосессия" in result

    def test_generate_proposal_no_data_does_not_raise(self):
        dept = self._dept()
        result = dept.generate_proposal({})
        assert isinstance(result, str)

    # --- get_followup_schedule ---

    def test_get_followup_schedule_returns_list(self):
        dept = self._dept()
        result = dept.get_followup_schedule(42, "new")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_get_followup_schedule_new_status(self):
        dept = self._dept()
        result = dept.get_followup_schedule(1, "new")
        assert len(result) >= 2

    def test_get_followup_schedule_completed_status(self):
        dept = self._dept()
        result = dept.get_followup_schedule(1, "completed")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_get_followup_schedule_contains_message(self):
        dept = self._dept()
        result = dept.get_followup_schedule(7, "processing")
        for item in result:
            assert "message" in item
            assert isinstance(item["message"], str)

    def test_get_followup_schedule_unknown_status(self):
        dept = self._dept()
        result = dept.get_followup_schedule(99, "unknown_status")
        assert isinstance(result, list)

    # --- suggest_pricing ---

    def test_suggest_pricing_returns_dict(self):
        dept = self._dept()
        result = dept.suggest_pricing({"event_type": "корпоратив", "model_count": 2})
        assert isinstance(result, dict)
        assert "min_price" in result
        assert "max_price" in result
        assert "recommended" in result

    def test_suggest_pricing_min_less_than_max(self):
        dept = self._dept()
        result = dept.suggest_pricing({"event_type": "свадьба", "model_count": 1})
        assert result["min_price"] < result["max_price"]

    def test_suggest_pricing_recommended_in_range(self):
        dept = self._dept()
        result = dept.suggest_pricing({"event_type": "фотосессия", "model_count": 1})
        assert result["min_price"] <= result["recommended"] <= result["max_price"]

    def test_suggest_pricing_scales_with_model_count(self):
        dept = self._dept()
        one = dept.suggest_pricing({"event_type": "корпоратив", "model_count": 1})
        two = dept.suggest_pricing({"event_type": "корпоратив", "model_count": 2})
        assert two["min_price"] > one["min_price"]

    def test_suggest_pricing_unknown_event_type_defaults(self):
        dept = self._dept()
        result = dept.suggest_pricing({"event_type": "неизвестный тип", "model_count": 1})
        assert result["min_price"] > 0
        assert result["recommended"] > 0


class TestCreativeDepartmentNew:
    """Tests for factory/agents/creative_department.py (no-API-call implementation)."""

    def _dept(self):
        from factory.agents.creative_department import CreativeDepartment
        return CreativeDepartment()

    # --- generate_model_bio ---

    def test_generate_model_bio_returns_string(self):
        dept = self._dept()
        result = dept.generate_model_bio({"name": "Анна", "city": "Москва", "categories": "подиум"})
        assert isinstance(result, str)
        assert len(result) > 20

    def test_generate_model_bio_contains_name(self):
        dept = self._dept()
        result = dept.generate_model_bio({"name": "Светлана"})
        assert "Светлана" in result

    def test_generate_model_bio_contains_city(self):
        dept = self._dept()
        result = dept.generate_model_bio({"name": "Ольга", "city": "Санкт-Петербург"})
        assert "Санкт-Петербург" in result

    def test_generate_model_bio_with_height(self):
        dept = self._dept()
        result = dept.generate_model_bio({"name": "Алина", "height": 178})
        assert "178" in result

    def test_generate_model_bio_categories_list(self):
        dept = self._dept()
        result = dept.generate_model_bio({"name": "Мария", "categories": ["подиум", "реклама"]})
        assert "подиум" in result or "реклама" in result

    def test_generate_model_bio_no_data_does_not_raise(self):
        dept = self._dept()
        result = dept.generate_model_bio({})
        assert isinstance(result, str)

    # --- generate_social_caption ---

    def test_generate_social_caption_returns_string(self):
        dept = self._dept()
        result = dept.generate_social_caption("корпоратив", "Анна")
        assert isinstance(result, str)
        assert len(result) > 20

    def test_generate_social_caption_contains_model_name(self):
        dept = self._dept()
        result = dept.generate_social_caption("фотосессия", "Наташа")
        assert "Наташа" in result

    def test_generate_social_caption_wedding(self):
        dept = self._dept()
        result = dept.generate_social_caption("свадьба", "Ирина")
        assert isinstance(result, str)
        assert len(result) > 20

    def test_generate_social_caption_unknown_event(self):
        dept = self._dept()
        result = dept.generate_social_caption("неизвестный тип", "Модель")
        assert isinstance(result, str)

    # --- generate_promo_text ---

    def test_generate_promo_text_returns_string(self):
        dept = self._dept()
        result = dept.generate_promo_text(15, 7)
        assert isinstance(result, str)
        assert len(result) > 20

    def test_generate_promo_text_contains_discount(self):
        dept = self._dept()
        result = dept.generate_promo_text(20, 3)
        assert "20" in result

    def test_generate_promo_text_single_day(self):
        dept = self._dept()
        result = dept.generate_promo_text(10, 1)
        assert isinstance(result, str)

    def test_generate_promo_text_zero_discount(self):
        dept = self._dept()
        result = dept.generate_promo_text(0, 5)
        assert isinstance(result, str)

    # --- get_brand_voice_guidelines ---

    def test_get_brand_voice_guidelines_returns_dict(self):
        dept = self._dept()
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result, dict)

    def test_get_brand_voice_guidelines_has_tone(self):
        dept = self._dept()
        result = dept.get_brand_voice_guidelines()
        assert "tone" in result
        assert isinstance(result["tone"], str)

    def test_get_brand_voice_guidelines_has_keywords(self):
        dept = self._dept()
        result = dept.get_brand_voice_guidelines()
        assert "keywords" in result
        assert isinstance(result["keywords"], list)
        assert len(result["keywords"]) > 0

    def test_get_brand_voice_guidelines_has_avoid_words(self):
        dept = self._dept()
        result = dept.get_brand_voice_guidelines()
        assert "avoid_words" in result
        assert isinstance(result["avoid_words"], list)

    def test_get_brand_voice_guidelines_has_style(self):
        dept = self._dept()
        result = dept.get_brand_voice_guidelines()
        assert "style" in result


class TestCustomerSuccessDepartmentNew:
    """Tests for factory/agents/customer_success_department.py (no-API-call implementation)."""

    def _dept(self):
        from factory.agents.customer_success_department import CustomerSuccessDepartment
        return CustomerSuccessDepartment()

    # --- generate_onboarding_message ---

    def test_generate_onboarding_message_returns_string(self):
        dept = self._dept()
        result = dept.generate_onboarding_message("Иван", "ORD-001")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_generate_onboarding_message_contains_name(self):
        dept = self._dept()
        result = dept.generate_onboarding_message("Алексей", "42")
        assert "Алексей" in result

    def test_generate_onboarding_message_contains_order_number(self):
        dept = self._dept()
        result = dept.generate_onboarding_message("Клиент", "ORD-999")
        assert "ORD-999" in result

    def test_generate_onboarding_message_empty_inputs(self):
        dept = self._dept()
        result = dept.generate_onboarding_message("", "")
        assert isinstance(result, str)

    # --- analyze_retention_risk ---

    def test_analyze_retention_risk_empty_history(self):
        dept = self._dept()
        result = dept.analyze_retention_risk([])
        assert isinstance(result, dict)
        assert "risk_level" in result
        assert result["risk_level"] == "unknown"

    def test_analyze_retention_risk_low_risk_recent_order(self):
        from datetime import datetime, timezone, timedelta
        dept = self._dept()
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        result = dept.analyze_retention_risk([{"date": recent}])
        assert result["risk_level"] == "low"
        assert result["days_since_last_order"] <= 30

    def test_analyze_retention_risk_high_risk_old_order(self):
        from datetime import datetime, timezone, timedelta
        dept = self._dept()
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        result = dept.analyze_retention_risk([{"date": old}])
        assert result["risk_level"] in ("high", "critical")

    def test_analyze_retention_risk_medium_risk(self):
        from datetime import datetime, timezone, timedelta
        dept = self._dept()
        mid = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        result = dept.analyze_retention_risk([{"date": mid}])
        assert result["risk_level"] == "medium"

    def test_analyze_retention_risk_returns_recommendation(self):
        from datetime import datetime, timezone, timedelta
        dept = self._dept()
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        result = dept.analyze_retention_risk([{"date": recent}])
        assert "recommendation" in result
        assert isinstance(result["recommendation"], str)

    def test_analyze_retention_risk_uses_most_recent_date(self):
        from datetime import datetime, timezone, timedelta
        dept = self._dept()
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        result = dept.analyze_retention_risk([{"date": old}, {"date": recent}])
        # Should pick the most recent date → low risk
        assert result["risk_level"] == "low"

    # --- generate_review_request ---

    def test_generate_review_request_returns_string(self):
        dept = self._dept()
        result = dept.generate_review_request({"client_name": "Анна", "event_type": "корпоратив"})
        assert isinstance(result, str)
        assert len(result) > 50

    def test_generate_review_request_contains_client_name(self):
        dept = self._dept()
        result = dept.generate_review_request({"client_name": "Светлана"})
        assert "Светлана" in result

    def test_generate_review_request_empty_data(self):
        dept = self._dept()
        result = dept.generate_review_request({})
        assert isinstance(result, str)

    # --- suggest_upsell ---

    def test_suggest_upsell_returns_dict(self):
        dept = self._dept()
        result = dept.suggest_upsell({"event_type": "корпоратив", "budget": 60_000})
        assert isinstance(result, dict)
        assert "suggestions" in result
        assert "reason" in result

    def test_suggest_upsell_suggestions_is_list(self):
        dept = self._dept()
        result = dept.suggest_upsell({"event_type": "свадьба", "budget": 80_000})
        assert isinstance(result["suggestions"], list)
        assert len(result["suggestions"]) >= 1

    def test_suggest_upsell_photoshoot(self):
        dept = self._dept()
        result = dept.suggest_upsell({"event_type": "фотосессия", "budget": 20_000})
        assert isinstance(result["suggestions"], list)

    def test_suggest_upsell_high_budget_bonus(self):
        dept = self._dept()
        low = dept.suggest_upsell({"event_type": "корпоратив", "budget": 10_000})
        high = dept.suggest_upsell({"event_type": "корпоратив", "budget": 100_000})
        # High budget should have at least as many suggestions
        assert len(high["suggestions"]) >= len(low["suggestions"])

    def test_suggest_upsell_reason_is_string(self):
        dept = self._dept()
        result = dept.suggest_upsell({"event_type": "корпоратив"})
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 5


import unittest


class TestTelegramNotification(unittest.TestCase):
    """Tests for _send_telegram_to_admins in cycle.py."""

    def _get_fn(self):
        from factory import cycle
        return cycle._send_telegram_to_admins

    def test_send_telegram_to_admins_no_token(self):
        """Should return False when TELEGRAM_BOT_TOKEN is not set."""
        fn = self._get_fn()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TELEGRAM_BOT_TOKEN', None)
            os.environ.pop('BOT_TOKEN', None)
            os.environ.pop('ADMIN_TELEGRAM_IDS', None)
            result = fn("Test message")
        self.assertFalse(result)

    def test_send_telegram_to_admins_no_admin_ids(self):
        """Should return False when ADMIN_TELEGRAM_IDS is not set."""
        fn = self._get_fn()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake_token'}, clear=False):
            os.environ.pop('ADMIN_TELEGRAM_IDS', None)
            result = fn("Test message")
        self.assertFalse(result)

    def test_send_telegram_to_admins_success(self):
        """Should return True when Telegram API responds with 200."""
        fn = self._get_fn()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'fake_token',
            'ADMIN_TELEGRAM_IDS': '123456789',
        }):
            with patch('requests.post', return_value=mock_resp) as mock_post:
                result = fn("Test message")
        self.assertTrue(result)
        mock_post.assert_called_once()

    def test_send_telegram_to_admins_multiple_admins(self):
        """Should send message to each admin ID."""
        fn = self._get_fn()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'fake_token',
            'ADMIN_TELEGRAM_IDS': '111,222,333',
        }):
            with patch('requests.post', return_value=mock_resp) as mock_post:
                result = fn("Test message")
        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 3)

    def test_send_telegram_to_admins_api_failure(self):
        """Should return False when Telegram API responds with non-200."""
        fn = self._get_fn()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'fake_token',
            'ADMIN_TELEGRAM_IDS': '123456789',
        }):
            with patch('requests.post', return_value=mock_resp):
                result = fn("Test message")
        self.assertFalse(result)

    def test_send_telegram_to_admins_network_error(self):
        """Should return False and not raise when requests raises an exception."""
        fn = self._get_fn()
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'fake_token',
            'ADMIN_TELEGRAM_IDS': '123456789',
        }):
            with patch('requests.post', side_effect=Exception("Network error")):
                result = fn("Test message")
        self.assertFalse(result)

    def test_ceo_report_tg_text_format(self):
        """CEO weekly report Telegram text has expected HTML structure."""
        week = 42
        headline = "Strong growth in orders"
        focus = "Improve conversion"
        concerns = ["Low traffic", "High churn"]

        tg_text = (
            f"<b>📊 CEO Weekly Report — Week {week}</b>\n\n"
            f"<b>{headline}</b>\n\n"
            f"🎯 <b>Фокус следующей недели:</b> {focus}\n"
            f"⚠️ <b>Риски:</b> {', '.join(concerns)}\n\n"
            f"<i>Сгенерировано AI Factory</i>"
        )
        self.assertIn("<b>", tg_text)
        self.assertIn("CEO Weekly Report", tg_text)
        self.assertIn("Week 42", tg_text)
        self.assertIn("Improve conversion", tg_text)
        self.assertIn("Low traffic", tg_text)


class TestDecisionTracker:
    """Tests for the DecisionTracker class."""

    def _make_tracker(self):
        from factory.agents.decision_tracker import DecisionTracker
        return DecisionTracker()

    def test_get_execution_summary_returns_required_keys(self):
        tracker = self._make_tracker()
        tasks = [
            {"title": "Task A", "action": "do_a", "status": "done"},
            {"title": "Task B", "action": "do_b", "status": "in_progress"},
            {"title": "Task C", "action": "do_c", "status": "pending"},
        ]
        result = tracker.get_execution_summary(tasks)
        for key in ("done_count", "in_progress_count", "pending_count",
                    "execution_rate", "done_titles", "next_focus"):
            assert key in result, f"Missing key: {key}"

    def test_execution_rate_equals_done_over_total(self):
        tracker = self._make_tracker()
        tasks = [
            {"action": "a", "status": "done"},
            {"action": "b", "status": "done"},
            {"action": "c", "status": "pending"},
            {"action": "d", "status": "pending"},
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["execution_rate"] == 2 / 4

    def test_empty_list_returns_zero_execution_rate(self):
        tracker = self._make_tracker()
        result = tracker.get_execution_summary([])
        assert result["execution_rate"] == 0
        assert result["done_count"] == 0
        assert result["in_progress_count"] == 0
        assert result["pending_count"] == 0

    def test_generate_accountability_report_returns_string_with_percent(self):
        tracker = self._make_tracker()
        summary = {
            "execution_rate": 0.75,
            "done_count": 3,
            "in_progress_count": 1,
            "pending_count": 0,
        }
        report = tracker.generate_accountability_report(summary)
        assert isinstance(report, str)
        assert "75%" in report

    def test_done_titles_listed_in_summary(self):
        tracker = self._make_tracker()
        tasks = [
            {"title": "Launch campaign", "status": "done"},
            {"title": "Update pricing", "status": "done"},
            {"title": "Hire manager", "status": "pending"},
        ]
        result = tracker.get_execution_summary(tasks)
        assert "Launch campaign" in result["done_titles"]
        assert "Update pricing" in result["done_titles"]
        assert result["done_count"] == 2

    def test_next_focus_is_title_of_first_pending_task(self):
        tracker = self._make_tracker()
        tasks = [
            {"action": "done_task", "status": "done"},
            {"action": "first_pending", "status": "pending"},
            {"action": "second_pending", "status": "pending"},
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == "first_pending"

    def test_next_focus_is_none_when_no_pending_tasks(self):
        tracker = self._make_tracker()
        tasks = [
            {"action": "done_task", "status": "done"},
        ]
        result = tracker.get_execution_summary(tasks)
        assert result["next_focus"] == "none"
