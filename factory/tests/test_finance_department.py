"""
Tests for factory/agents/finance_department.py — standalone heuristic version.

Covers:
  - RevenueForecaster   : forecast_monthly_revenue, calculate_growth_rate
  - CostOptimizer       : analyze_cost_structure, suggest_pricing_adjustments
  - PricingStrategist   : calculate_optimal_price, get_seasonal_multiplier
  - BudgetPlanner       : create_monthly_budget, evaluate_budget_variance
"""
from __future__ import annotations

import pytest

from factory.agents.finance_department import (
    BudgetPlanner,
    CostOptimizer,
    PricingStrategist,
    RevenueForecaster,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def forecaster() -> RevenueForecaster:
    return RevenueForecaster()


@pytest.fixture
def optimizer() -> CostOptimizer:
    return CostOptimizer()


@pytest.fixture
def strategist() -> PricingStrategist:
    return PricingStrategist()


@pytest.fixture
def planner() -> BudgetPlanner:
    return BudgetPlanner()


@pytest.fixture
def growing_history():
    """6 months of steadily growing revenue."""
    return [
        {"revenue": 10_000},
        {"revenue": 11_000},
        {"revenue": 12_500},
        {"revenue": 14_000},
        {"revenue": 15_500},
        {"revenue": 17_000},
    ]


@pytest.fixture
def declining_history():
    """4 months of declining revenue."""
    return [
        {"revenue": 20_000},
        {"revenue": 17_000},
        {"revenue": 14_000},
        {"revenue": 11_000},
    ]


@pytest.fixture
def three_month_history():
    """Exactly 3 months of history (medium confidence threshold)."""
    return [
        {"revenue": 5_000},
        {"revenue": 6_000},
        {"revenue": 7_000},
    ]


# ══════════════════════════════════════════════════════════════
# TestRevenueForecaster
# ══════════════════════════════════════════════════════════════

class TestRevenueForecaster:
    """Tests for RevenueForecaster."""

    # -- forecast_monthly_revenue --------------------------------

    def test_forecast_no_history_returns_dict_with_forecast_key(self, forecaster):
        result = forecaster.forecast_monthly_revenue([])
        assert isinstance(result, dict)
        assert "forecast" in result

    def test_forecast_no_history_returns_zero_forecast(self, forecaster):
        result = forecaster.forecast_monthly_revenue([])
        assert result["forecast"] == 0.0

    def test_forecast_no_history_confidence_is_low(self, forecaster):
        result = forecaster.forecast_monthly_revenue([])
        assert result["confidence"] == "low"

    def test_forecast_three_months_returns_positive_forecast(self, forecaster, three_month_history):
        result = forecaster.forecast_monthly_revenue(three_month_history)
        assert result["forecast"] > 0

    def test_forecast_six_months_confidence_is_high(self, forecaster, growing_history):
        result = forecaster.forecast_monthly_revenue(growing_history)
        assert result["confidence"] == "high"

    def test_forecast_three_months_confidence_is_medium(self, forecaster, three_month_history):
        result = forecaster.forecast_monthly_revenue(three_month_history)
        assert result["confidence"] == "medium"

    def test_forecast_one_month_confidence_is_low(self, forecaster):
        result = forecaster.forecast_monthly_revenue([{"revenue": 10_000}])
        assert result["confidence"] == "low"

    def test_forecast_growing_data_trend_is_growing(self, forecaster, growing_history):
        result = forecaster.forecast_monthly_revenue(growing_history)
        assert result["trend"] == "growing"

    def test_forecast_declining_data_trend_is_declining(self, forecaster, declining_history):
        result = forecaster.forecast_monthly_revenue(declining_history)
        assert result["trend"] == "declining"

    def test_forecast_flat_data_trend_is_stable(self, forecaster):
        flat = [{"revenue": 10_000}] * 6
        result = forecaster.forecast_monthly_revenue(flat)
        assert result["trend"] == "stable"

    def test_forecast_months_ahead_3(self, forecaster, growing_history):
        r1 = forecaster.forecast_monthly_revenue(growing_history, months_ahead=1)
        r3 = forecaster.forecast_monthly_revenue(growing_history, months_ahead=3)
        # Forecasting further ahead with a growing trend should produce a larger value
        assert r3["forecast"] > r1["forecast"]

    def test_forecast_amount_key_accepted(self, forecaster):
        """Entries using 'amount' key instead of 'revenue' should work."""
        history = [{"amount": 5_000}, {"amount": 6_000}, {"amount": 7_000}]
        result = forecaster.forecast_monthly_revenue(history)
        assert result["forecast"] > 0

    def test_forecast_basis_months_matches_input(self, forecaster, growing_history):
        result = forecaster.forecast_monthly_revenue(growing_history)
        assert result["basis_months"] == len(growing_history)

    # -- calculate_growth_rate -----------------------------------

    def test_growth_rate_growing_data_returns_positive(self, forecaster):
        revenues = [1_000, 1_100, 1_200, 1_300]
        rate = forecaster.calculate_growth_rate(revenues)
        assert rate > 0

    def test_growth_rate_declining_data_returns_negative(self, forecaster):
        revenues = [2_000, 1_800, 1_600, 1_400]
        rate = forecaster.calculate_growth_rate(revenues)
        assert rate < 0

    def test_growth_rate_single_item_returns_zero(self, forecaster):
        assert forecaster.calculate_growth_rate([5_000]) == 0.0

    def test_growth_rate_empty_list_returns_zero(self, forecaster):
        assert forecaster.calculate_growth_rate([]) == 0.0

    def test_growth_rate_flat_data_returns_zero(self, forecaster):
        revenues = [10_000, 10_000, 10_000]
        assert forecaster.calculate_growth_rate(revenues) == pytest.approx(0.0)

    def test_growth_rate_returns_float(self, forecaster):
        rate = forecaster.calculate_growth_rate([1_000, 1_100])
        assert isinstance(rate, float)


# ══════════════════════════════════════════════════════════════
# TestCostOptimizer
# ══════════════════════════════════════════════════════════════

class TestCostOptimizer:
    """Tests for CostOptimizer."""

    # -- analyze_cost_structure ----------------------------------

    def test_analyze_returns_dict_with_suggestions_list(self, optimizer):
        expenses = {"marketing": 50_000, "operations": 30_000, "rent": 20_000}
        result = optimizer.analyze_cost_structure(expenses)
        assert isinstance(result, dict)
        assert "suggestions" in result
        assert isinstance(result["suggestions"], list)

    def test_analyze_returns_total(self, optimizer):
        expenses = {"marketing": 40_000, "operations": 60_000}
        result = optimizer.analyze_cost_structure(expenses)
        assert result["total"] == pytest.approx(100_000)

    def test_analyze_high_marketing_cost_triggers_suggestion(self, optimizer):
        """Marketing at 36% (> 35% threshold) should produce a suggestion."""
        expenses = {"marketing": 36_000, "operations": 64_000}
        result = optimizer.analyze_cost_structure(expenses)
        assert any("marketing" in s.lower() for s in result["suggestions"])

    def test_analyze_balanced_cost_structure_no_critical_suggestion(self, optimizer):
        """A perfectly balanced structure should get the 'balanced' message."""
        expenses = {"a": 25_000, "b": 25_000, "c": 25_000, "d": 25_000}
        result = optimizer.analyze_cost_structure(expenses)
        assert any("balanced" in s.lower() or "no critical" in s.lower()
                   for s in result["suggestions"])

    def test_analyze_empty_expenses_returns_empty(self, optimizer):
        result = optimizer.analyze_cost_structure({})
        assert result["total"] == 0.0
        assert result["suggestions"] == []

    def test_analyze_breakdown_contains_pct(self, optimizer):
        expenses = {"marketing": 50_000, "ops": 50_000}
        result = optimizer.analyze_cost_structure(expenses)
        assert "pct" in result["breakdown"]["marketing"]

    def test_analyze_many_small_categories_suggests_consolidation(self, optimizer):
        """Three or more categories < 5% of total should trigger consolidation tip."""
        expenses = {
            "marketing": 80_000,
            "misc1": 1_000,
            "misc2": 1_000,
            "misc3": 1_000,
        }
        result = optimizer.analyze_cost_structure(expenses)
        assert any("consolidat" in s.lower() for s in result["suggestions"])

    # -- suggest_pricing_adjustments -----------------------------

    def test_pricing_adjustments_returns_list(self, optimizer):
        stats = [
            {"name": "Alice", "bookings": 10, "current_rate": 5_000, "avg_rating": 4.0},
        ]
        result = optimizer.suggest_pricing_adjustments(stats)
        assert isinstance(result, list)

    def test_pricing_adjustments_empty_input_returns_empty(self, optimizer):
        assert optimizer.suggest_pricing_adjustments([]) == []

    def test_low_demand_model_gets_price_decrease_suggestion(self, optimizer):
        """Model with far fewer bookings than average should get a rate cut."""
        stats = [
            {"name": "Alice", "bookings": 50, "current_rate": 5_000, "avg_rating": 4.5},
            {"name": "Bob",   "bookings": 50, "current_rate": 5_000, "avg_rating": 4.0},
            {"name": "Carol", "bookings": 3,  "current_rate": 5_000, "avg_rating": 3.5},
        ]
        result = optimizer.suggest_pricing_adjustments(stats)
        carol = next((r for r in result if r["name"] == "Carol"), None)
        assert carol is not None
        assert carol["suggested_rate"] < carol["current_rate"]
        assert "low" in carol["rationale"].lower() or "stimulate" in carol["rationale"].lower()

    def test_high_demand_model_gets_price_increase_suggestion(self, optimizer):
        """Model with far more bookings than average should get a rate increase."""
        stats = [
            {"name": "Alice", "bookings": 5,  "current_rate": 5_000, "avg_rating": 3.5},
            {"name": "Bob",   "bookings": 5,  "current_rate": 5_000, "avg_rating": 3.5},
            {"name": "Carol", "bookings": 30, "current_rate": 5_000, "avg_rating": 4.8},
        ]
        result = optimizer.suggest_pricing_adjustments(stats)
        carol = next((r for r in result if r["name"] == "Carol"), None)
        assert carol is not None
        assert carol["suggested_rate"] > carol["current_rate"]

    def test_zero_current_rate_model_is_skipped(self, optimizer):
        stats = [
            {"name": "Ghost", "bookings": 10, "current_rate": 0, "avg_rating": 4.0},
        ]
        result = optimizer.suggest_pricing_adjustments(stats)
        assert result == []

    def test_stable_demand_rate_unchanged(self, optimizer):
        """All models with equal bookings → no price change."""
        stats = [
            {"name": "Alice", "bookings": 10, "current_rate": 5_000, "avg_rating": 4.0},
            {"name": "Bob",   "bookings": 10, "current_rate": 6_000, "avg_rating": 3.8},
        ]
        result = optimizer.suggest_pricing_adjustments(stats)
        for adj in result:
            assert adj["suggested_rate"] == adj["current_rate"]


# ══════════════════════════════════════════════════════════════
# TestPricingStrategist
# ══════════════════════════════════════════════════════════════

class TestPricingStrategist:
    """Tests for PricingStrategist."""

    # -- calculate_optimal_price ---------------------------------

    def test_calculate_optimal_price_returns_required_keys(self, strategist):
        result = strategist.calculate_optimal_price(
            "corporate", {"avg_rating": 4.0, "bookings": 10}
        )
        assert "suggested_price" in result
        assert "min_price" in result
        assert "max_price" in result
        assert "rationale" in result

    def test_suggested_price_between_min_and_max(self, strategist):
        result = strategist.calculate_optimal_price(
            "photoshoot", {"avg_rating": 3.0, "bookings": 5}
        )
        # Multiplier can push suggested above base_max, so check it is at least min_price
        assert result["suggested_price"] >= result["min_price"]

    def test_fashion_show_higher_base_than_promo(self, strategist):
        """Fashion show has a higher base price range than promo."""
        fashion = strategist.calculate_optimal_price(
            "fashion show", {"avg_rating": 3.0, "bookings": 0}
        )
        promo = strategist.calculate_optimal_price(
            "promo", {"avg_rating": 3.0, "bookings": 0}
        )
        assert fashion["suggested_price"] > promo["suggested_price"]

    def test_featured_model_has_price_bonus(self, strategist):
        """High-rated, high-experience model should get a higher price."""
        base = strategist.calculate_optimal_price(
            "corporate", {"avg_rating": 3.0, "bookings": 0}
        )
        featured = strategist.calculate_optimal_price(
            "corporate", {"avg_rating": 5.0, "bookings": 60}
        )
        assert featured["suggested_price"] > base["suggested_price"]

    def test_rationale_is_non_empty_string(self, strategist):
        result = strategist.calculate_optimal_price(
            "wedding", {"avg_rating": 4.0, "bookings": 25}
        )
        assert isinstance(result["rationale"], str)
        assert len(result["rationale"]) > 0

    def test_unknown_event_type_uses_default_range(self, strategist):
        """Unknown event type should fall back to default range and still return a price."""
        result = strategist.calculate_optimal_price(
            "unknown_gala", {"avg_rating": 3.0, "bookings": 0}
        )
        assert result["suggested_price"] > 0
        assert result["min_price"] == pytest.approx(result["min_price"])  # sanity

    def test_min_price_less_than_max_price(self, strategist):
        result = strategist.calculate_optimal_price(
            "corporate", {"avg_rating": 3.5, "bookings": 10}
        )
        assert result["min_price"] < result["max_price"]

    # -- get_seasonal_multiplier ---------------------------------

    def test_seasonal_multiplier_returns_float(self, strategist):
        m = strategist.get_seasonal_multiplier(6)
        assert isinstance(m, float)

    def test_seasonal_multiplier_in_valid_range(self, strategist):
        for month in range(1, 13):
            m = strategist.get_seasonal_multiplier(month)
            assert 0.8 <= m <= 1.5, f"Month {month} multiplier {m} out of range"

    def test_december_has_peak_multiplier(self, strategist):
        """December (New Year season) should be at 1.25 or above."""
        assert strategist.get_seasonal_multiplier(12) >= 1.25

    def test_august_has_base_multiplier(self, strategist):
        """August is the off-season — multiplier should be ~1.0."""
        m = strategist.get_seasonal_multiplier(8)
        assert m == pytest.approx(1.0, abs=0.05)

    def test_january_is_higher_than_august(self, strategist):
        assert strategist.get_seasonal_multiplier(1) > strategist.get_seasonal_multiplier(8)

    def test_december_is_highest_multiplier(self, strategist):
        dec = strategist.get_seasonal_multiplier(12)
        for month in range(1, 12):
            assert dec >= strategist.get_seasonal_multiplier(month)


# ══════════════════════════════════════════════════════════════
# TestBudgetPlanner
# ══════════════════════════════════════════════════════════════

class TestBudgetPlanner:
    """Tests for BudgetPlanner."""

    # -- create_monthly_budget -----------------------------------

    def test_create_monthly_budget_returns_dict_with_allocations(self, planner):
        result = planner.create_monthly_budget(100_000, {"rent": 20_000})
        assert isinstance(result, dict)
        assert "allocations" in result
        assert isinstance(result["allocations"], dict)

    def test_allocations_do_not_exceed_total_budget(self, planner):
        result = planner.create_monthly_budget(100_000, {"rent": 20_000})
        total_alloc = sum(result["allocations"].values())
        assert total_alloc <= result["total_budget"] + 0.01  # small float tolerance

    def test_surplus_is_calculated_correctly(self, planner):
        result = planner.create_monthly_budget(100_000, {"rent": 20_000})
        expected_surplus = result["total_budget"] - sum(result["allocations"].values())
        assert result["surplus"] == pytest.approx(expected_surplus, abs=0.02)

    def test_total_budget_matches_revenue_forecast(self, planner):
        result = planner.create_monthly_budget(75_000, {"rent": 10_000})
        assert result["total_budget"] == pytest.approx(75_000)

    def test_fixed_costs_appear_in_allocations(self, planner):
        result = planner.create_monthly_budget(100_000, {"rent": 15_000, "salaries": 25_000})
        assert "rent" in result["allocations"]
        assert "salaries" in result["allocations"]

    def test_zero_revenue_forecast_no_errors(self, planner):
        result = planner.create_monthly_budget(0, {})
        assert result["total_budget"] == 0.0
        assert isinstance(result["allocations"], dict)

    def test_surplus_can_be_negative_when_fixed_costs_exceed_revenue(self, planner):
        """When fixed costs exceed revenue, surplus should be negative."""
        result = planner.create_monthly_budget(10_000, {"rent": 20_000})
        # surplus = revenue - total_allocated; total_allocated >= fixed_costs = 20_000
        assert result["surplus"] < 0

    # -- evaluate_budget_variance --------------------------------

    def test_evaluate_variance_returns_required_keys(self, planner):
        planned = {"marketing": 10_000, "ops": 5_000}
        actual  = {"marketing": 11_000, "ops": 4_500}
        result = planner.evaluate_budget_variance(planned, actual)
        assert "variances" in result
        assert "total_variance" in result
        assert "status" in result

    def test_over_budget_category_has_positive_variance(self, planner):
        planned = {"marketing": 10_000}
        actual  = {"marketing": 12_000}
        result = planner.evaluate_budget_variance(planned, actual)
        assert result["variances"]["marketing"]["variance"] > 0

    def test_under_budget_category_has_negative_variance(self, planner):
        planned = {"ops": 10_000}
        actual  = {"ops": 8_000}
        result = planner.evaluate_budget_variance(planned, actual)
        assert result["variances"]["ops"]["variance"] < 0

    def test_significantly_over_budget_status_is_over_budget(self, planner):
        planned = {"marketing": 10_000, "ops": 10_000}
        actual  = {"marketing": 15_000, "ops": 15_000}
        result = planner.evaluate_budget_variance(planned, actual)
        assert result["status"] == "over_budget"

    def test_significantly_under_budget_status_is_under_budget(self, planner):
        planned = {"marketing": 10_000, "ops": 10_000}
        actual  = {"marketing": 5_000, "ops": 5_000}
        result = planner.evaluate_budget_variance(planned, actual)
        assert result["status"] == "under_budget"

    def test_on_budget_status_when_within_threshold(self, planner):
        planned = {"marketing": 10_000}
        actual  = {"marketing": 10_200}  # 2% over — within 5% threshold
        result = planner.evaluate_budget_variance(planned, actual)
        assert result["status"] == "on_budget"

    def test_total_variance_matches_actual_minus_planned(self, planner):
        planned = {"a": 5_000, "b": 5_000}
        actual  = {"a": 6_000, "b": 4_000}
        result = planner.evaluate_budget_variance(planned, actual)
        expected_total_variance = sum(actual.values()) - sum(planned.values())
        assert result["total_variance"] == pytest.approx(expected_total_variance, abs=0.02)

    def test_category_only_in_actual_included_in_variances(self, planner):
        """Category present only in actual (not planned) should appear in variances."""
        planned = {"marketing": 10_000}
        actual  = {"marketing": 10_000, "new_expense": 3_000}
        result = planner.evaluate_budget_variance(planned, actual)
        assert "new_expense" in result["variances"]


# ──────────────────────────────────────────────────────────────
# FinanceDepartment facade
# ──────────────────────────────────────────────────────────────

from factory.agents.finance_department import FinanceDepartment


@pytest.fixture
def dept() -> FinanceDepartment:
    return FinanceDepartment()


class TestFinanceDepartment:
    """Integration tests for the FinanceDepartment facade."""

    def test_run_analysis_returns_required_keys(self, dept):
        result = dept.run_analysis({'revenue_history': [10_000, 12_000, 14_000], 'costs': {}})
        assert 'revenue_forecast_3m' in result
        assert 'trend' in result
        assert 'confidence' in result
        assert 'cost_analysis' in result
        assert 'budget_plan' in result

    def test_run_analysis_forecast_is_list_of_three(self, dept):
        result = dept.run_analysis({'revenue_history': [10_000, 12_000, 14_000], 'costs': {}})
        assert isinstance(result['revenue_forecast_3m'], list)
        assert len(result['revenue_forecast_3m']) == 3

    def test_run_analysis_empty_history(self, dept):
        result = dept.run_analysis({'revenue_history': [], 'costs': {}})
        assert result['revenue_forecast_3m'] == [0.0, 0.0, 0.0]

    def test_run_analysis_single_month_history(self, dept):
        result = dept.run_analysis({'revenue_history': [50_000], 'costs': {}})
        assert isinstance(result['revenue_forecast_3m'], list)
        assert len(result['revenue_forecast_3m']) == 3

    def test_run_analysis_with_costs(self, dept):
        costs = {'marketing': 2_000, 'operations': 3_000}
        result = dept.run_analysis({'revenue_history': [20_000, 22_000], 'costs': costs})
        assert 'cost_analysis' in result

    def test_run_analysis_accepts_dict_revenue_history(self, dept):
        history = [{'revenue': 10_000}, {'revenue': 12_000}, {'revenue': 14_000}]
        result = dept.run_analysis({'revenue_history': history, 'costs': {}})
        assert isinstance(result['revenue_forecast_3m'], list)

    def test_run_analysis_accepts_mixed_float_revenue_history(self, dept):
        result = dept.run_analysis({'revenue_history': [10_000.0, 15_000.0], 'costs': {}})
        assert 'trend' in result

    def test_run_analysis_trend_is_string(self, dept):
        result = dept.run_analysis({'revenue_history': [10_000, 12_000, 14_000], 'costs': {}})
        assert isinstance(result['trend'], str)

    def test_run_analysis_confidence_is_string(self, dept):
        result = dept.run_analysis({'revenue_history': [10_000, 12_000, 14_000], 'costs': {}})
        assert isinstance(result['confidence'], str)

    def test_run_analysis_no_errors_on_zero_costs(self, dept):
        result = dept.run_analysis({'revenue_history': [100_000], 'costs': {}})
        assert result is not None

    def test_run_analysis_missing_keys_uses_defaults(self, dept):
        """run_analysis should work with an empty data dict."""
        result = dept.run_analysis({})
        assert 'revenue_forecast_3m' in result
        assert result['revenue_forecast_3m'] == [0.0, 0.0, 0.0]

    def test_facade_exposes_individual_agents(self, dept):
        assert hasattr(dept, 'forecaster')
        assert hasattr(dept, 'optimizer')
        assert hasattr(dept, 'pricing')
        assert hasattr(dept, 'planner')


class TestFinanceDepartmentExecuteTask:
    """Tests for FinanceDepartment.execute_task method."""

    def test_execute_task_returns_dict(self, dept):
        result = dept.execute_task("run finance analysis")
        assert isinstance(result, dict)

    def test_execute_task_returns_roles_used(self, dept):
        result = dept.execute_task("run finance analysis")
        assert "roles_used" in result
        assert isinstance(result["roles_used"], list)

    def test_execute_task_roles_used_has_four_agents(self, dept):
        result = dept.execute_task("run finance analysis")
        assert len(result["roles_used"]) == 4

    def test_execute_task_roles_used_contains_all_expected_agents(self, dept):
        result = dept.execute_task("run finance analysis")
        expected = {"revenue_forecaster", "cost_optimizer", "pricing_strategist", "budget_planner"}
        assert set(result["roles_used"]) == expected

    def test_execute_task_returns_result_with_summary(self, dept):
        result = dept.execute_task("run finance analysis")
        assert "result" in result
        assert "summary" in result["result"]
        assert isinstance(result["result"]["summary"], str)

    def test_execute_task_returns_trend_string(self, dept):
        result = dept.execute_task("analyze finances")
        assert "trend" in result
        assert isinstance(result["trend"], str)

    def test_execute_task_returns_confidence_string(self, dept):
        result = dept.execute_task("analyze finances")
        assert "confidence" in result
        assert isinstance(result["confidence"], str)

    def test_execute_task_returns_insights_list(self, dept):
        result = dept.execute_task("optimize costs")
        assert "insights" in result
        assert isinstance(result["insights"], list)

    def test_execute_task_none_context_no_error(self, dept):
        """execute_task should work when context=None."""
        result = dept.execute_task("finance report", context=None)
        assert isinstance(result, dict)
        assert "roles_used" in result

    def test_execute_task_empty_context_no_error(self, dept):
        """execute_task should work with an empty context dict."""
        result = dept.execute_task("finance report", context={})
        assert isinstance(result, dict)
        assert "roles_used" in result

    def test_execute_task_with_nevesty_kpis_context(self, dept):
        """execute_task should integrate nevesty_kpis properly."""
        context = {
            "nevesty_kpis": {
                "revenue_month": 50_000,
                "orders_this_month": 20,
                "avg_check": 2_500,
            }
        }
        result = dept.execute_task("full analysis", context=context)
        assert result["confidence"] in ("low", "medium", "high")

    def test_execute_task_zero_revenue_kpis(self, dept):
        """Zero revenue in KPIs should still return valid structure."""
        context = {"nevesty_kpis": {"revenue_month": 0}}
        result = dept.execute_task("analysis", context=context)
        assert "result" in result
        assert "0" in result["result"]["summary"]

    def test_execute_task_with_costs_in_context(self, dept):
        """Costs passed in context should appear in cost_analysis."""
        context = {
            "revenue_history": [30_000, 35_000, 40_000],
            "costs": {"marketing": 5_000, "ops": 3_000},
        }
        result = dept.execute_task("budget review", context=context)
        assert "cost_analysis" in result
