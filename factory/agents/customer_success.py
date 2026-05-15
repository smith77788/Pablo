"""Customer Success Department — Nevesty Models Factory"""
from __future__ import annotations
import datetime
from factory.agents.base import FactoryAgent


class OnboardingSpecialist(FactoryAgent):
    """Helps new clients get started with the agency."""
    department = "customer_success"
    role = "OnboardingSpecialist"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        metrics = {}
        if data_db:
            try:
                row = data_db.execute("""
                    SELECT COUNT(*) as new_clients,
                           COUNT(CASE WHEN status='completed' THEN 1 END) as converted
                    FROM orders
                    WHERE created_at >= date('now', '-30 days')
                """).fetchone()
                metrics = dict(row) if row else {}
            except Exception:
                pass

        analysis = self.think(
            f"Analyze new client onboarding for modeling agency. "
            f"Last 30 days: {metrics}. "
            f"Provide: 1) onboarding improvement suggestions, 2) common friction points, "
            f"3) welcome message optimization, 4) first-order conversion tips.",
            context={"metrics": metrics, "period": "30d"}
        )
        return {"role": self.role, "analysis": analysis, "metrics": metrics}


class RetentionAnalyst(FactoryAgent):
    """Analyzes client retention and churn patterns."""
    department = "customer_success"
    role = "RetentionAnalyst"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        retention_data = {}
        if data_db:
            try:
                # Clients with repeat orders
                row = data_db.execute("""
                    SELECT
                        COUNT(DISTINCT client_chat_id) as total_clients,
                        COUNT(DISTINCT CASE WHEN order_count > 1 THEN client_chat_id END) as repeat_clients
                    FROM (
                        SELECT client_chat_id, COUNT(*) as order_count
                        FROM orders WHERE client_chat_id IS NOT NULL
                        GROUP BY client_chat_id
                    )
                """).fetchone()
                retention_data = dict(row) if row else {}
            except Exception:
                pass

        analysis = self.think(
            f"Analyze client retention for modeling agency. "
            f"Data: {retention_data}. "
            f"Provide: 1) retention rate assessment, 2) churn reduction strategies, "
            f"3) loyalty program suggestions, 4) re-engagement campaign ideas.",
            context=retention_data
        )
        return {"role": self.role, "analysis": analysis, "retention_data": retention_data}


class FeedbackCollector(FactoryAgent):
    """Designs feedback collection strategies."""
    department = "customer_success"
    role = "FeedbackCollector"

    def run(self, **kwargs) -> dict:
        data_db = kwargs.get("data_db")
        review_data = {}
        if data_db:
            try:
                row = data_db.execute("""
                    SELECT AVG(rating) as avg_rating, COUNT(*) as total_reviews,
                           COUNT(CASE WHEN rating >= 4 THEN 1 END) as positive
                    FROM reviews WHERE approved=1
                """).fetchone()
                review_data = dict(row) if row else {}
            except Exception:
                pass

        analysis = self.think(
            f"Design feedback collection strategy for modeling agency. "
            f"Current reviews data: {review_data}. "
            f"Suggest: 1) optimal timing for review requests, 2) review prompt templates, "
            f"3) incentives for leaving reviews, 4) how to handle negative feedback.",
            context=review_data
        )
        return {"role": self.role, "analysis": analysis, "review_data": review_data}
