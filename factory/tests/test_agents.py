"""
Basic tests for Factory agents.
These tests verify that agents initialize correctly and produce output.
"""
import sys
import os
import pytest

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


class TestSalesDepartment:
    """Tests for Sales department agents."""

    def test_sales_module_imports(self):
        from factory.agents.sales import LeadQualifier, ProposalWriter, FollowUpSpecialist
        assert LeadQualifier is not None
        assert ProposalWriter is not None
        assert FollowUpSpecialist is not None

    def test_lead_qualifier_init(self):
        from factory.agents.sales import LeadQualifier
        agent = LeadQualifier()
        assert agent.name == 'LeadQualifier'
        assert agent.department == 'sales'

    def test_proposal_writer_init(self):
        from factory.agents.sales import ProposalWriter
        agent = ProposalWriter()
        assert agent.name == 'ProposalWriter'

    def test_follow_up_specialist_init(self):
        from factory.agents.sales import FollowUpSpecialist
        agent = FollowUpSpecialist()
        assert agent.name == 'FollowUpSpecialist'

    def test_lead_qualifier_has_run_method(self):
        from factory.agents.sales import LeadQualifier
        agent = LeadQualifier()
        assert hasattr(agent, 'run')
        assert callable(agent.run)

    def test_lead_qualifier_has_build_prompt(self):
        from factory.agents.sales import LeadQualifier
        agent = LeadQualifier()
        assert hasattr(agent, 'build_prompt')
        # build_prompt should return a string even with no DB data
        prompt = agent.build_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0


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


class TestCyclePhase21:
    """Tests for Phase 21: CEO Weekly Summary."""

    def test_phase_21_weekly_report_in_cycle(self):
        """Phase 21 exists in cycle module."""
        import factory.cycle as cycle
        src = open(cycle.__file__).read()
        assert 'Phase 21' in src
