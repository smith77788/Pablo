"""Research Department — Nevesty Models Factory"""
from __future__ import annotations
import datetime
from factory.agents.base import FactoryAgent


class MarketResearcher(FactoryAgent):
    """Researches modeling agency market trends and opportunities."""
    department = "research"
    role = "MarketResearcher"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        market_data = {}
        if data_db:
            try:
                # Get category distribution
                rows = data_db.execute("""
                    SELECT category, COUNT(*) as cnt
                    FROM orders
                    WHERE created_at >= date('now', '-30 days')
                    GROUP BY category ORDER BY cnt DESC
                """).fetchall()
                market_data['categories'] = [dict(r) for r in rows]

                # Get event type distribution
                rows2 = data_db.execute("""
                    SELECT event_type, COUNT(*) as cnt
                    FROM orders
                    WHERE created_at >= date('now', '-30 days') AND event_type IS NOT NULL
                    GROUP BY event_type ORDER BY cnt DESC LIMIT 5
                """).fetchall()
                market_data['event_types'] = [dict(r) for r in rows2]
            except Exception:
                pass

        analysis = self.think(
            f"Research modeling agency market for {datetime.date.today().strftime('%B %Y')}. "
            f"Our data: {market_data}. "
            f"Analyze: 1) market demand trends, 2) most requested event types, "
            f"3) category opportunities, 4) seasonal patterns, "
            f"5) recommendations for agency positioning.",
            context=market_data
        )
        return {"role": self.role, "analysis": analysis, "market_data": market_data}


class TrendSpotter(FactoryAgent):
    """Identifies emerging trends in the modeling industry."""
    department = "research"
    role = "TrendSpotter"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        trend_data = {}
        if data_db:
            try:
                # Week-over-week comparison
                this_week = data_db.execute("""
                    SELECT COUNT(*) as cnt FROM orders
                    WHERE created_at >= date('now', '-7 days')
                """).fetchone()
                last_week = data_db.execute("""
                    SELECT COUNT(*) as cnt FROM orders
                    WHERE created_at BETWEEN date('now', '-14 days') AND date('now', '-7 days')
                """).fetchone()

                trend_data['this_week'] = this_week['cnt'] if this_week else 0
                trend_data['last_week'] = last_week['cnt'] if last_week else 0
                trend_data['wow_change'] = (
                    round((trend_data['this_week'] - trend_data['last_week']) / max(trend_data['last_week'], 1) * 100, 1)
                    if trend_data['last_week'] > 0 else 0
                )

                # Popular models this month vs last
                hot_rows = data_db.execute("""
                    SELECT m.name, COUNT(o.id) as recent_orders
                    FROM models m JOIN orders o ON o.model_id = m.id
                    WHERE o.created_at >= date('now', '-14 days')
                    GROUP BY m.id ORDER BY recent_orders DESC LIMIT 3
                """).fetchall()
                trend_data['hot_models'] = [dict(r) for r in hot_rows]
            except Exception:
                pass

        analysis = self.think(
            f"Spot emerging trends for modeling agency. "
            f"Week-over-week data: {trend_data}. "
            f"Identify: 1) demand growth/decline trends, 2) hot models and why they're trending, "
            f"3) upcoming seasonal opportunities, 4) content and marketing trend recommendations.",
            context=trend_data
        )
        return {"role": self.role, "analysis": analysis, "trend_data": trend_data}


class InsightSynthesizer(FactoryAgent):
    """Synthesizes insights from all departments into actionable recommendations."""
    department = "research"
    role = "InsightSynthesizer"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        all_data = {}
        if data_db:
            try:
                summary = data_db.execute("""
                    SELECT
                        COUNT(*) as total_orders,
                        COUNT(CASE WHEN status='completed' THEN 1 END) as completed,
                        COUNT(CASE WHEN status='new' THEN 1 END) as new_orders,
                        COUNT(DISTINCT client_chat_id) as unique_clients
                    FROM orders WHERE created_at >= date('now', '-30 days')
                """).fetchone()
                all_data = dict(summary) if summary else {}
            except Exception:
                pass

        analysis = self.think(
            f"Synthesize key business insights for modeling agency leadership. "
            f"30-day summary: {all_data}. "
            f"Produce: 1) top 3 actionable insights, 2) biggest opportunity this week, "
            f"3) one thing to stop/start/continue, 4) KPI to focus on next cycle.",
            context=all_data
        )
        return {"role": self.role, "analysis": analysis, "data": all_data}
