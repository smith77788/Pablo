"""
Finance Department — standalone heuristic agents (no API calls).

Agents:
  - RevenueForecaster   : forecast revenue from historical order data
  - CostOptimizer       : analyze costs and suggest optimizations
  - PricingStrategist   : dynamic pricing strategy
  - BudgetPlanner       : budget planning and variance tracking
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# RevenueForecaster
# ──────────────────────────────────────────────────────────────

class RevenueForecaster:
    """Forecasts revenue based on historical order data."""

    def forecast_monthly_revenue(
        self,
        orders_history: list[dict[str, Any]],
        months_ahead: int = 1,
    ) -> dict[str, Any]:
        """Forecast revenue for next N months based on trends.

        Returns:
            {
                forecast: float,
                confidence: str,          # 'high' | 'medium' | 'low'
                trend: str,               # 'growing' | 'stable' | 'declining'
                basis_months: int,
            }
        """
        if not orders_history:
            return {
                "forecast": 0.0,
                "confidence": "low",
                "trend": "stable",
                "basis_months": 0,
            }

        # Extract monthly revenue totals; each entry may have 'revenue' or 'amount'
        monthly_revenues: list[float] = []
        for entry in orders_history:
            rev = entry.get("revenue") or entry.get("amount") or 0.0
            try:
                monthly_revenues.append(float(rev))
            except (TypeError, ValueError):
                pass

        basis_months = len(monthly_revenues)
        if basis_months == 0:
            return {
                "forecast": 0.0,
                "confidence": "low",
                "trend": "stable",
                "basis_months": 0,
            }

        avg_revenue = sum(monthly_revenues) / basis_months
        growth_rate = self.calculate_growth_rate(monthly_revenues)

        # Project forward N months with compound growth
        forecast = avg_revenue * ((1.0 + growth_rate) ** months_ahead)

        # Confidence based on amount of history
        if basis_months >= 6:
            confidence = "high"
        elif basis_months >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # Trend label
        if growth_rate > 0.03:
            trend = "growing"
        elif growth_rate < -0.03:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "forecast": round(forecast, 2),
            "confidence": confidence,
            "trend": trend,
            "basis_months": basis_months,
        }

    def calculate_growth_rate(self, monthly_revenues: list[float]) -> float:
        """Calculate average month-over-month growth rate.

        Returns 0.0 if fewer than 2 data points.
        """
        if len(monthly_revenues) < 2:
            return 0.0

        growth_rates: list[float] = []
        for i in range(1, len(monthly_revenues)):
            prev = monthly_revenues[i - 1]
            curr = monthly_revenues[i]
            if prev > 0:
                growth_rates.append((curr - prev) / prev)
            # Skip if previous is zero (avoid division by zero)

        if not growth_rates:
            return 0.0

        return sum(growth_rates) / len(growth_rates)


# ──────────────────────────────────────────────────────────────
# CostOptimizer
# ──────────────────────────────────────────────────────────────

class CostOptimizer:
    """Analyzes costs and suggests optimizations."""

    # Thresholds: if a category exceeds this % of total spend → flag it
    HIGH_SPEND_THRESHOLD_PCT = 0.35

    def analyze_cost_structure(
        self, expenses: dict[str, float]
    ) -> dict[str, Any]:
        """Analyze expense categories and find optimization opportunities.

        Returns:
            {
                total: float,
                breakdown: {category: {amount, pct}},
                suggestions: [str, ...],
            }
        """
        total = sum(expenses.values())
        if total == 0:
            return {"total": 0.0, "breakdown": {}, "suggestions": []}

        breakdown: dict[str, dict[str, Any]] = {}
        suggestions: list[str] = []

        for category, amount in expenses.items():
            pct = amount / total
            breakdown[category] = {
                "amount": round(amount, 2),
                "pct": round(pct * 100, 1),
            }
            if pct > self.HIGH_SPEND_THRESHOLD_PCT:
                suggestions.append(
                    f"'{category}' consumes {pct*100:.1f}% of budget — review for reduction"
                )

        # Suggest consolidation when many small categories exist
        small_categories = [c for c, v in expenses.items() if v / total < 0.05]
        if len(small_categories) >= 3:
            suggestions.append(
                f"Consider consolidating {len(small_categories)} small cost lines "
                f"({', '.join(small_categories[:3])}...) to reduce overhead"
            )

        if not suggestions:
            suggestions.append("Cost structure looks balanced — no critical optimizations found")

        return {
            "total": round(total, 2),
            "breakdown": breakdown,
            "suggestions": suggestions,
        }

    def suggest_pricing_adjustments(
        self, model_stats: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Suggest price adjustments per model based on demand.

        Each entry in model_stats should contain:
            { name: str, bookings: int, current_rate: float, avg_rating: float }

        Returns list of adjustment dicts:
            { name: str, current_rate: float, suggested_rate: float, rationale: str }
        """
        adjustments: list[dict[str, Any]] = []

        if not model_stats:
            return adjustments

        # Calculate average bookings to establish a baseline
        all_bookings = [m.get("bookings", 0) for m in model_stats]
        avg_bookings = sum(all_bookings) / len(all_bookings) if all_bookings else 1.0

        for model in model_stats:
            name = model.get("name", "Unknown")
            bookings = float(model.get("bookings", 0))
            current_rate = float(model.get("current_rate", 0))
            avg_rating = float(model.get("avg_rating", 3.0))

            if current_rate <= 0:
                continue

            # High demand (>30% above avg) → raise price 10–20%
            if bookings > avg_bookings * 1.3:
                multiplier = 1.15 if avg_rating >= 4.5 else 1.10
                rationale = "High demand — increase rate to capture value"
            # Low demand (<50% of avg) → lower price 10%
            elif bookings < avg_bookings * 0.5:
                multiplier = 0.90
                rationale = "Low demand — reduce rate to stimulate bookings"
            else:
                multiplier = 1.0
                rationale = "Demand is stable — no adjustment needed"

            suggested_rate = round(current_rate * multiplier, 2)

            adjustments.append(
                {
                    "name": name,
                    "current_rate": current_rate,
                    "suggested_rate": suggested_rate,
                    "rationale": rationale,
                }
            )

        return adjustments


# ──────────────────────────────────────────────────────────────
# PricingStrategist
# ──────────────────────────────────────────────────────────────

class PricingStrategist:
    """Sets dynamic pricing strategies."""

    # Base price ranges (min, max) per event type in RUB
    _EVENT_BASE_PRICES: dict[str, tuple[int, int]] = {
        "corporate": (15_000, 35_000),
        "корпоратив": (15_000, 35_000),
        "wedding": (20_000, 50_000),
        "свадьба": (20_000, 50_000),
        "photoshoot": (10_000, 25_000),
        "фотосессия": (10_000, 25_000),
        "fashion show": (25_000, 60_000),
        "показ": (25_000, 60_000),
        "promo": (8_000, 20_000),
        "промо": (8_000, 20_000),
    }
    _DEFAULT_RANGE = (12_000, 30_000)

    def calculate_optimal_price(
        self,
        event_type: str,
        model_data: dict[str, Any],
        market_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Calculate optimal price for a booking.

        Returns:
            {
                suggested_price: float,
                min_price: float,
                max_price: float,
                rationale: str,
            }
        """
        event_key = event_type.lower()

        # Find matching price range
        base_min, base_max = self._DEFAULT_RANGE
        for key, (bmin, bmax) in self._EVENT_BASE_PRICES.items():
            if key in event_key:
                base_min, base_max = bmin, bmax
                break

        # Seasonal adjustment
        import datetime
        month = datetime.date.today().month
        seasonal = self.get_seasonal_multiplier(month)
        base_min = round(base_min * seasonal)
        base_max = round(base_max * seasonal)

        # Rating premium: each star above 3.0 adds 5%
        avg_rating = float(model_data.get("avg_rating", 3.0))
        rating_bonus = max(0.0, (avg_rating - 3.0) * 0.05)

        # Experience premium: >50 bookings → +10%
        bookings = int(model_data.get("bookings", 0))
        experience_bonus = 0.10 if bookings > 50 else 0.05 if bookings > 20 else 0.0

        multiplier = 1.0 + rating_bonus + experience_bonus
        suggested = round(((base_min + base_max) / 2) * multiplier)

        rationale_parts = []
        if seasonal != 1.0:
            rationale_parts.append(f"seasonal factor {seasonal:.2f}x")
        if rating_bonus > 0:
            rationale_parts.append(f"rating premium +{rating_bonus*100:.0f}%")
        if experience_bonus > 0:
            rationale_parts.append(f"experience bonus +{experience_bonus*100:.0f}%")
        rationale = (
            "Price based on: " + ", ".join(rationale_parts)
            if rationale_parts
            else "Standard market rate for this event type"
        )

        return {
            "suggested_price": float(suggested),
            "min_price": float(base_min),
            "max_price": float(base_max),
            "rationale": rationale,
        }

    def get_seasonal_multiplier(self, month: int) -> float:
        """Get seasonal price multiplier (1.0 = normal, 1.2 = peak season).

        Peak seasons for modeling agency in Russia:
          Dec–Jan (New Year/Christmas), Feb (Valentine's), May (spring events),
          Jun–Jul (summer weddings).
        """
        multipliers: dict[int, float] = {
            1: 1.20,   # January — post NY premium
            2: 1.15,   # February — Valentine's
            3: 1.05,
            4: 1.05,
            5: 1.15,   # May — spring corporates
            6: 1.20,   # June — weddings peak
            7: 1.15,   # July — summer
            8: 1.00,
            9: 1.05,   # September — back-to-business
            10: 1.05,
            11: 1.10,  # November — pre-NY ramp-up
            12: 1.25,  # December — peak NY season
        }
        return multipliers.get(month, 1.0)


# ──────────────────────────────────────────────────────────────
# BudgetPlanner
# ──────────────────────────────────────────────────────────────

class BudgetPlanner:
    """Plans budgets and tracks spending."""

    # Default allocation ratios when no overrides are given
    _DEFAULT_ALLOCATIONS = {
        "marketing": 0.35,
        "operations": 0.30,
        "development": 0.15,
        "reserve": 0.10,
        "other": 0.10,
    }

    def create_monthly_budget(
        self,
        revenue_forecast: float,
        fixed_costs: dict[str, float],
    ) -> dict[str, Any]:
        """Create a monthly budget plan.

        Returns:
            {
                total_budget: float,
                allocations: {category: float},
                surplus: float,
            }
        """
        total_fixed = sum(fixed_costs.values())

        # Allocatable budget = revenue_forecast minus fixed costs
        allocatable = max(0.0, revenue_forecast - total_fixed)

        allocations: dict[str, float] = {}

        # Include fixed costs as their own line items
        for category, amount in fixed_costs.items():
            allocations[category] = round(amount, 2)

        # Distribute remaining budget by default ratios
        for category, ratio in self._DEFAULT_ALLOCATIONS.items():
            if category not in allocations:
                allocations[category] = round(allocatable * ratio, 2)

        total_allocated = sum(allocations.values())
        surplus = round(revenue_forecast - total_allocated, 2)

        return {
            "total_budget": round(revenue_forecast, 2),
            "allocations": allocations,
            "surplus": surplus,
        }

    def evaluate_budget_variance(
        self,
        planned: dict[str, float],
        actual: dict[str, float],
    ) -> dict[str, Any]:
        """Compare planned vs actual spend.

        Returns:
            {
                variances: {category: {planned, actual, variance, variance_pct}},
                total_variance: float,
                status: str,   # 'on_budget' | 'over_budget' | 'under_budget'
            }
        """
        all_categories = set(planned.keys()) | set(actual.keys())
        variances: dict[str, dict[str, Any]] = {}

        for cat in all_categories:
            p = float(planned.get(cat, 0.0))
            a = float(actual.get(cat, 0.0))
            variance = a - p
            variance_pct = (variance / p * 100.0) if p != 0 else (100.0 if a > 0 else 0.0)
            variances[cat] = {
                "planned": round(p, 2),
                "actual": round(a, 2),
                "variance": round(variance, 2),
                "variance_pct": round(variance_pct, 1),
            }

        total_planned = sum(planned.values())
        total_actual = sum(actual.values())
        total_variance = round(total_actual - total_planned, 2)

        if total_variance > total_planned * 0.05:
            status = "over_budget"
        elif total_variance < -total_planned * 0.05:
            status = "under_budget"
        else:
            status = "on_budget"

        return {
            "variances": variances,
            "total_variance": total_variance,
            "status": status,
        }


# ──────────────────────────────────────────────────────────────
# FinanceDepartment — facade
# ──────────────────────────────────────────────────────────────

class FinanceDepartment:
    """Facade that orchestrates all Finance Department heuristic agents."""

    def __init__(self) -> None:
        self.forecaster = RevenueForecaster()
        self.optimizer = CostOptimizer()
        self.pricing = PricingStrategist()
        self.planner = BudgetPlanner()

    def execute_task(self, task: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a finance task using all department agents.

        Adapts the context dict (may include nevesty_kpis) into run_analysis format.
        """
        ctx = context or {}
        kpis = ctx.get("nevesty_kpis", {})
        monthly_revenue = float(kpis.get("revenue_month") or 0.0)
        orders_month = int(kpis.get("orders_this_month") or 0)

        # Build revenue history from KPIs if no explicit history provided
        revenue_history = ctx.get("revenue_history", [])
        if not revenue_history and monthly_revenue > 0:
            revenue_history = [{"revenue": monthly_revenue}]

        costs = ctx.get("costs", {})

        analysis = self.run_analysis({"revenue_history": revenue_history, "costs": costs})

        forecast_3m = analysis.get("revenue_forecast_3m", [0.0, 0.0, 0.0])
        forecast_next = forecast_3m[0] if forecast_3m else 0.0

        return {
            "roles_used": ["revenue_forecaster", "cost_optimizer", "pricing_strategist", "budget_planner"],
            "revenue_forecast": analysis,
            "trend": analysis.get("trend", "stable"),
            "confidence": analysis.get("confidence", "low"),
            "cost_analysis": analysis.get("cost_analysis", {}),
            "budget_plan": analysis.get("budget_plan", {}),
            "insights": analysis.get("cost_analysis", {}).get("suggestions", []),
            "result": {
                "summary": (
                    f"Finance cycle: 3-month forecast {forecast_next:.0f}, "
                    f"trend={analysis.get('trend', 'stable')}, "
                    f"confidence={analysis.get('confidence', 'low')}"
                )
            },
        }

    def run_analysis(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run full financial analysis cycle.

        Args:
            data: dict with keys:
                revenue_history: list of monthly revenue dicts or floats
                costs: dict of category -> amount (actual costs)

        Returns:
            Aggregated analysis results.
        """
        revenue_history_raw: list = data.get('revenue_history', [])
        costs: dict[str, float] = data.get('costs', {})

        # Normalize revenue history to list-of-dicts format expected by forecaster
        orders_history: list[dict[str, Any]] = []
        for entry in revenue_history_raw:
            if isinstance(entry, dict):
                orders_history.append(entry)
            else:
                orders_history.append({'revenue': float(entry)})

        forecast_result = self.forecaster.forecast_monthly_revenue(orders_history, months_ahead=3)
        current_revenue = forecast_result.get('forecast', 0.0)

        cost_analysis = self.optimizer.analyze_cost_structure(
            expenses=costs,
        ) if costs else {'suggestions': [], 'breakdown': {}}

        budget = self.planner.create_monthly_budget(
            revenue_forecast=current_revenue,
            fixed_costs=costs,
        )

        return {
            'revenue_forecast_3m': [
                self.forecaster.forecast_monthly_revenue(orders_history, months_ahead=i).get('forecast', 0.0)
                for i in range(1, 4)
            ],
            'trend': forecast_result.get('trend', 'stable'),
            'confidence': forecast_result.get('confidence', 'low'),
            'cost_analysis': cost_analysis,
            'budget_plan': budget,
        }
