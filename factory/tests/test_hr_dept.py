"""
Tests for factory/agents/hr_dept.py — HR / Model Department.

Covers:
  - CandidateScreener  : department, run() contract
  - PortfolioEvaluator : department, run() contract
  - RankingSystem      : department, run() contract
  - HRDepartment       : execute_task() facade
"""
from __future__ import annotations

import pytest

from factory.agents.hr_dept import (
    CandidateScreener,
    HRDepartment,
    PortfolioEvaluator,
    RankingSystem,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def screener() -> CandidateScreener:
    return CandidateScreener()


@pytest.fixture
def evaluator() -> PortfolioEvaluator:
    return PortfolioEvaluator()


@pytest.fixture
def ranker() -> RankingSystem:
    return RankingSystem()


@pytest.fixture
def dept() -> HRDepartment:
    return HRDepartment()


@pytest.fixture
def strong_candidate():
    return {"experience_years": 5, "rating": 4.8, "portfolio_size": 35}


@pytest.fixture
def weak_candidate():
    return {"experience_years": 0, "rating": 2.0, "portfolio_size": 5}


@pytest.fixture
def rich_portfolio_model():
    return {"photo_count": 40, "categories": ["fashion", "commercial", "wedding"]}


@pytest.fixture
def thin_portfolio_model():
    return {"photo_count": 5, "categories": ["fashion"]}


@pytest.fixture
def sample_models():
    return [
        {"id": 1, "name": "Alice", "category": "fashion", "available": 1},
        {"id": 2, "name": "Bob",   "category": "commercial", "available": 1},
        {"id": 3, "name": "Carol", "category": "events", "available": 0},
    ]


# ══════════════════════════════════════════════════════════════
# TestCandidateScreener
# ══════════════════════════════════════════════════════════════

class TestCandidateScreener:
    """Tests for CandidateScreener agent."""

    def test_department_is_hr(self, screener):
        assert screener.department == "hr"

    def test_instantiation(self, screener):
        assert isinstance(screener, CandidateScreener)

    def test_run_returns_dict(self, screener):
        result = screener.run()
        assert isinstance(result, dict)

    def test_run_none_context_no_error(self, screener):
        result = screener.run(None)
        assert isinstance(result, dict)

    def test_run_has_insights_key(self, screener):
        result = screener.run()
        assert "insights" in result
        assert isinstance(result["insights"], list)

    def test_run_insights_non_empty(self, screener):
        result = screener.run()
        assert len(result["insights"]) >= 1

    def test_run_has_timestamp(self, screener):
        result = screener.run()
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)
        # ISO 8601 format check
        assert "T" in result["timestamp"]

    def test_run_strong_candidate_verdict_accept(self, screener, strong_candidate):
        result = screener.run({"candidate": strong_candidate})
        assert result["verdict"] == "accept"

    def test_run_weak_candidate_verdict_reject(self, screener, weak_candidate):
        result = screener.run({"candidate": weak_candidate})
        assert result["verdict"] == "reject"

    def test_run_strong_candidate_has_positive_insight(self, screener, strong_candidate):
        result = screener.run({"candidate": strong_candidate})
        assert any("accept" in s.lower() or "strong" in s.lower() for s in result["insights"])

    def test_run_large_portfolio_mentions_commitment(self, screener, strong_candidate):
        result = screener.run({"candidate": strong_candidate})
        assert any("portfolio" in s.lower() for s in result["insights"])

    def test_run_score_is_integer(self, screener, strong_candidate):
        result = screener.run({"candidate": strong_candidate})
        assert isinstance(result["score"], int)

    def test_run_score_in_valid_range(self, screener, strong_candidate):
        result = screener.run({"candidate": strong_candidate})
        assert 1 <= result["score"] <= 10

    def test_run_empty_context_no_error(self, screener):
        result = screener.run({})
        assert isinstance(result, dict)
        assert "insights" in result


# ══════════════════════════════════════════════════════════════
# TestPortfolioEvaluator
# ══════════════════════════════════════════════════════════════

class TestPortfolioEvaluator:
    """Tests for PortfolioEvaluator agent."""

    def test_department_is_hr(self, evaluator):
        assert evaluator.department == "hr"

    def test_instantiation(self, evaluator):
        assert isinstance(evaluator, PortfolioEvaluator)

    def test_run_returns_dict(self, evaluator):
        result = evaluator.run()
        assert isinstance(result, dict)

    def test_run_none_context_no_error(self, evaluator):
        result = evaluator.run(None)
        assert isinstance(result, dict)

    def test_run_has_insights_key(self, evaluator):
        result = evaluator.run()
        assert "insights" in result
        assert isinstance(result["insights"], list)

    def test_run_has_timestamp(self, evaluator):
        result = evaluator.run()
        assert "timestamp" in result
        assert "T" in result["timestamp"]

    def test_run_rich_portfolio_quality_high(self, evaluator, rich_portfolio_model):
        result = evaluator.run({"model": rich_portfolio_model})
        assert result["photo_quality"] == "high"

    def test_run_thin_portfolio_quality_low(self, evaluator, thin_portfolio_model):
        result = evaluator.run({"model": thin_portfolio_model})
        assert result["photo_quality"] == "low"

    def test_run_rich_portfolio_versatility_high(self, evaluator, rich_portfolio_model):
        result = evaluator.run({"model": rich_portfolio_model})
        assert result["versatility"] == "high"

    def test_run_thin_portfolio_versatility_low(self, evaluator, thin_portfolio_model):
        result = evaluator.run({"model": thin_portfolio_model})
        assert result["versatility"] == "low"

    def test_run_portfolio_score_is_int(self, evaluator, rich_portfolio_model):
        result = evaluator.run({"model": rich_portfolio_model})
        assert isinstance(result["portfolio_score"], int)

    def test_run_portfolio_score_in_valid_range(self, evaluator, rich_portfolio_model):
        result = evaluator.run({"model": rich_portfolio_model})
        assert 1 <= result["portfolio_score"] <= 10

    def test_run_empty_model_no_error(self, evaluator):
        result = evaluator.run({"model": {}})
        assert isinstance(result, dict)
        assert "insights" in result


# ══════════════════════════════════════════════════════════════
# TestRankingSystem
# ══════════════════════════════════════════════════════════════

class TestRankingSystem:
    """Tests for RankingSystem agent."""

    def test_department_is_hr(self, ranker):
        assert ranker.department == "hr"

    def test_instantiation(self, ranker):
        assert isinstance(ranker, RankingSystem)

    def test_run_returns_dict(self, ranker):
        result = ranker.run()
        assert isinstance(result, dict)

    def test_run_none_context_no_error(self, ranker):
        result = ranker.run(None)
        assert isinstance(result, dict)

    def test_run_has_insights_key(self, ranker):
        result = ranker.run()
        assert "insights" in result
        assert isinstance(result["insights"], list)

    def test_run_has_timestamp(self, ranker):
        result = ranker.run()
        assert "timestamp" in result
        assert "T" in result["timestamp"]

    def test_run_empty_models_gives_empty_rankings(self, ranker):
        result = ranker.run({"models": []})
        assert result["rankings"] == []
        assert result["total_ranked"] == 0

    def test_run_with_models_returns_rankings(self, ranker, sample_models):
        result = ranker.run({"models": sample_models})
        assert isinstance(result["rankings"], list)
        assert len(result["rankings"]) == len(sample_models)

    def test_run_rankings_have_required_keys(self, ranker, sample_models):
        result = ranker.run({"models": sample_models})
        for entry in result["rankings"]:
            assert "model_id" in entry
            assert "rank" in entry
            assert "score" in entry
            assert "action" in entry

    def test_run_total_ranked_matches_models(self, ranker, sample_models):
        result = ranker.run({"models": sample_models})
        assert result["total_ranked"] == len(sample_models)

    def test_run_first_model_gets_promote(self, ranker, sample_models):
        result = ranker.run({"models": sample_models})
        top = result["rankings"][0]
        assert top["action"] == "promote"

    def test_run_empty_context_no_error(self, ranker):
        result = ranker.run({})
        assert isinstance(result, dict)
        assert "insights" in result


# ══════════════════════════════════════════════════════════════
# TestHRDepartment
# ══════════════════════════════════════════════════════════════

class TestHRDepartment:
    """Tests for HRDepartment facade."""

    def test_instantiation(self, dept):
        assert isinstance(dept, HRDepartment)

    def test_has_screener_agent(self, dept):
        assert hasattr(dept, "screener")
        assert isinstance(dept.screener, CandidateScreener)

    def test_has_portfolio_agent(self, dept):
        assert hasattr(dept, "portfolio")
        assert isinstance(dept.portfolio, PortfolioEvaluator)

    def test_has_ranker_agent(self, dept):
        assert hasattr(dept, "ranker")
        assert isinstance(dept.ranker, RankingSystem)

    def test_execute_task_returns_dict(self, dept):
        result = dept.execute_task("run hr analysis")
        assert isinstance(result, dict)

    def test_execute_task_has_roles_used(self, dept):
        result = dept.execute_task("run hr analysis")
        assert "roles_used" in result
        assert isinstance(result["roles_used"], list)

    def test_execute_task_roles_used_count_gte_2(self, dept):
        result = dept.execute_task("run hr analysis")
        assert len(result["roles_used"]) >= 2

    def test_execute_task_roles_used_has_all_agents(self, dept):
        result = dept.execute_task("run hr analysis")
        expected = {"candidate_screener", "portfolio_evaluator", "ranking_system"}
        assert set(result["roles_used"]) == expected

    def test_execute_task_has_insights(self, dept):
        result = dept.execute_task("run hr analysis")
        assert "insights" in result
        assert isinstance(result["insights"], list)

    def test_execute_task_insights_non_empty(self, dept):
        result = dept.execute_task("run hr analysis")
        # At minimum one insight from each agent (no-context path)
        assert len(result["insights"]) >= 1

    def test_execute_task_has_timestamp(self, dept):
        result = dept.execute_task("run hr analysis")
        assert "timestamp" in result
        assert "T" in result["timestamp"]

    def test_execute_task_none_context_no_error(self, dept):
        result = dept.execute_task("hr task", context=None)
        assert isinstance(result, dict)
        assert "roles_used" in result

    def test_execute_task_empty_context_no_error(self, dept):
        result = dept.execute_task("hr task", context={})
        assert isinstance(result, dict)
        assert "insights" in result

    def test_execute_task_with_candidate_context(self, dept, strong_candidate):
        result = dept.execute_task("screen candidate", context={"candidate": strong_candidate})
        assert isinstance(result, dict)
        screening = result.get("screening", {})
        assert screening.get("verdict") == "accept"

    def test_execute_task_with_model_context(self, dept, rich_portfolio_model):
        result = dept.execute_task("evaluate portfolio", context={"model": rich_portfolio_model})
        portfolio = result.get("portfolio", {})
        assert portfolio.get("photo_quality") == "high"

    def test_execute_task_with_models_context(self, dept, sample_models):
        result = dept.execute_task("rank models", context={"models": sample_models})
        rankings_section = result.get("rankings", {})
        assert rankings_section.get("total_ranked") == len(sample_models)

    def test_execute_task_insights_aggregate_from_all_agents(self, dept):
        """Insights should aggregate contributions from all three agents."""
        result = dept.execute_task("full hr review")
        # screener + portfolio + ranker each contributes >= 2 insights when no models given
        assert len(result["insights"]) >= 4
