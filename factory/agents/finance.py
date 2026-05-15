"""Finance Department — Nevesty Models Factory"""
from __future__ import annotations
import datetime
from factory.agents.base import FactoryAgent


class RevenueForecaster(FactoryAgent):
    """Forecasts revenue trends based on historical data."""
    department = "finance"
    role = "RevenueForecaster"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        trend_data = []
        if data_db:
            try:
                rows = data_db.execute("""
                    SELECT strftime('%Y-%m', created_at) as month,
                           COUNT(*) as orders,
                           COUNT(CASE WHEN status='completed' THEN 1 END) as completed
                    FROM orders
                    WHERE created_at >= date('now', '-6 months')
                    GROUP BY month ORDER BY month
                """).fetchall()
                trend_data = [dict(r) for r in rows]
            except Exception:
                pass

        analysis = self.think(
            f"Forecast revenue trends for modeling agency based on order data. "
            f"6-month trend: {trend_data}. "
            f"Provide: 1) revenue trajectory assessment, 2) next month forecast, "
            f"3) seasonal patterns, 4) growth opportunities.",
            context={"trend": trend_data}
        )
        return {"role": self.role, "analysis": analysis, "trend_data": trend_data}


class PricingStrategist(FactoryAgent):
    """Optimizes pricing strategy for maximum revenue."""
    department = "finance"
    role = "PricingStrategist"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        pricing_data = {}
        if data_db:
            try:
                row = data_db.execute("""
                    SELECT
                        COUNT(*) as total_orders,
                        AVG(CAST(REPLACE(REPLACE(budget, '₽', ''), ' ', '') AS REAL)) as avg_budget
                    FROM orders
                    WHERE budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'
                      AND created_at >= date('now', '-90 days')
                """).fetchone()
                pricing_data = dict(row) if row else {}
            except Exception:
                pass

        analysis = self.think(
            f"Optimize pricing strategy for modeling agency. "
            f"Last 90 days data: {pricing_data}. "
            f"Suggest: 1) optimal price points per service tier, 2) package deals, "
            f"3) dynamic pricing opportunities, 4) upsell strategies.",
            context=pricing_data
        )
        return {"role": self.role, "analysis": analysis, "pricing_data": pricing_data}
