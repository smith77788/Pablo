"""📊 Analytics Engine — собирает метрики, генерирует инсайты."""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)

NEVESTY_DB = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


class AnalyticsEngine(FactoryAgent):
    name = "analytics_engine"
    system_prompt = """Ты — Analytics Engine AI Startup Factory.
Твоя роль: анализировать метрики всех продуктов и выдавать конкретные инсайты.

ПРАВИЛА:
- Всегда отвечай на русском
- Выдавай конкретные числа, не абстракции
- Указывай тренды: рост/падение в %
- Выделяй ТОП проблему и ТОП возможность
- Будь краток — максимум 5 инсайтов"""

    # ─── Real metrics from Nevesty Models SQLite ─────────────────────────────

    def _collect_nevesty_metrics(self) -> dict:
        """Read real business metrics from the Nevesty bot database."""
        metrics = {}
        if not NEVESTY_DB.exists():
            return metrics
        try:
            conn = sqlite3.connect(str(NEVESTY_DB))
            conn.row_factory = sqlite3.Row

            # Orders metrics
            orders_total = conn.execute("SELECT COUNT(*) as n FROM orders").fetchone()["n"]
            orders_new = conn.execute("SELECT COUNT(*) as n FROM orders WHERE status='new'").fetchone()["n"]
            orders_confirmed = conn.execute("SELECT COUNT(*) as n FROM orders WHERE status='confirmed'").fetchone()["n"]
            orders_completed = conn.execute("SELECT COUNT(*) as n FROM orders WHERE status='completed'").fetchone()["n"]
            orders_cancelled = conn.execute("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'").fetchone()["n"]
            orders_7d = conn.execute(
                "SELECT COUNT(*) as n FROM orders WHERE created_at > datetime('now','-7 days')"
            ).fetchone()["n"]
            orders_prev7d = conn.execute(
                "SELECT COUNT(*) as n FROM orders WHERE created_at BETWEEN datetime('now','-14 days') AND datetime('now','-7 days')"
            ).fetchone()["n"]

            # Models metrics
            models_active = conn.execute("SELECT COUNT(*) as n FROM models WHERE available=1").fetchone()["n"]
            models_with_photo = conn.execute(
                "SELECT COUNT(*) as n FROM models WHERE available=1 AND photo_main IS NOT NULL AND photo_main != ''"
            ).fetchone()["n"]

            # Bot sessions (users)
            users_total = conn.execute("SELECT COUNT(*) as n FROM telegram_sessions").fetchone()["n"]
            users_active_7d = conn.execute(
                "SELECT COUNT(*) as n FROM telegram_sessions WHERE updated_at > datetime('now','-7 days')"
            ).fetchone()["n"]

            # Revenue (sum of budgets for confirmed+completed)
            revenue_row = conn.execute(
                "SELECT SUM(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER)) as total FROM orders "
                "WHERE status IN ('confirmed','in_progress','completed') AND budget IS NOT NULL AND budget != ''"
            ).fetchone()
            revenue_total = revenue_row["total"] or 0

            revenue_30d_row = conn.execute(
                "SELECT SUM(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER)) as total FROM orders "
                "WHERE status IN ('confirmed','in_progress','completed') AND budget IS NOT NULL AND budget != '' "
                "AND created_at > datetime('now','-30 days')"
            ).fetchone()
            revenue_30d = revenue_30d_row["total"] or 0

            # Average deal cycle (days from new to completed)
            avg_cycle_row = conn.execute(
                "SELECT AVG(CAST(julianday(updated_at) - julianday(created_at) AS REAL)) as avg_days "
                "FROM orders WHERE status='completed'"
            ).fetchone()
            avg_deal_days = round(avg_cycle_row["avg_days"] or 0, 1)

            # Repeat clients (phones with 2+ orders)
            repeat_clients_row = conn.execute(
                "SELECT COUNT(*) as n FROM ("
                "  SELECT client_phone FROM orders WHERE client_phone IS NOT NULL "
                "  GROUP BY client_phone HAVING COUNT(*) >= 2"
                ")"
            ).fetchone()
            repeat_clients = repeat_clients_row["n"]

            # Top categories
            top_categories = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM orders GROUP BY event_type ORDER BY cnt DESC LIMIT 3"
            ).fetchall()
            category_breakdown = {row["event_type"]: row["cnt"] for row in top_categories if row["event_type"]}

            # Top models by orders
            top_models = conn.execute(
                "SELECT m.name, COUNT(o.id) as order_count, "
                "ROUND(AVG(r.rating), 1) as avg_rating "
                "FROM orders o JOIN models m ON o.model_id = m.id "
                "LEFT JOIN reviews r ON r.model_id = m.id AND r.approved = 1 "
                "GROUP BY m.id ORDER BY order_count DESC LIMIT 5"
            ).fetchall()
            top_models_list = [
                {"name": row["name"], "orders": row["order_count"], "rating": row["avg_rating"]}
                for row in top_models
            ]

            # Reviews stats
            reviews_total = conn.execute("SELECT COUNT(*) as n FROM reviews WHERE approved=1").fetchone()["n"]
            reviews_avg = conn.execute(
                "SELECT ROUND(AVG(rating), 2) as avg FROM reviews WHERE approved=1"
            ).fetchone()["avg"] or 0

            # Conversion new→confirmed
            confirmed_from_new = round(orders_confirmed / max(orders_new + orders_confirmed, 1) * 100, 1)

            conn.close()

            conversion = round(orders_total / max(users_total, 1) * 100, 2)
            orders_growth = round((orders_7d - orders_prev7d) / max(orders_prev7d, 1) * 100, 1)

            metrics = {
                "orders_total": orders_total,
                "orders_new": orders_new,
                "orders_confirmed": orders_confirmed,
                "orders_completed": orders_completed,
                "orders_cancelled": orders_cancelled,
                "orders_7d": orders_7d,
                "orders_7d_prev": orders_prev7d,
                "orders_growth_pct": orders_growth,
                "models_active": models_active,
                "models_with_photo": models_with_photo,
                "models_photo_coverage_pct": round(models_with_photo / max(models_active, 1) * 100, 1),
                "users_total": users_total,
                "users_active_7d": users_active_7d,
                "conversion_rate_pct": conversion,
                "revenue_total": revenue_total,
                "revenue_30d": revenue_30d,
                "avg_deal_days": avg_deal_days,
                "repeat_clients": repeat_clients,
                "repeat_client_rate_pct": round(repeat_clients / max(orders_total, 1) * 100, 1),
                "category_breakdown": category_breakdown,
                "top_models": top_models_list,
                "reviews_total": reviews_total,
                "reviews_avg_rating": reviews_avg,
                "conversion_new_to_confirmed_pct": confirmed_from_new,
            }
        except Exception as e:
            logger.warning("Cannot read Nevesty DB: %s", e)
        return metrics

    def _collect_factory_metrics(self) -> dict:
        """Collect metrics from all factory products."""
        products = db.get_active_products()
        experiments = db.get_running_experiments()
        decisions = db.get_recent_decisions(10)

        product_metrics = []
        for p in products:
            raw = db.get_product_metrics(p["id"], limit=14)
            by_name: dict[str, list] = {}
            for m in raw:
                by_name.setdefault(m["metric_name"], []).append(m["value"])
            product_metrics.append({
                "id": p["id"],
                "name": p["name"],
                "status": p["status"],
                "category": p["category"],
                "metrics": {k: {"latest": v[0], "avg": round(sum(v)/len(v), 2)} for k, v in by_name.items()},
            })

        return {
            "products": product_metrics,
            "running_experiments": len(experiments),
            "recent_decisions": [d["decision_type"] for d in decisions],
        }

    def collect_all_metrics(self) -> dict:
        nevesty = self._collect_nevesty_metrics()
        factory = self._collect_factory_metrics()
        return {"nevesty_models": nevesty, "factory": factory}

    def persist_nevesty_metrics(self, nevesty_product_id: int, metrics: dict) -> None:
        """Save Nevesty metrics to the factory DB for trend tracking."""
        fields = [
            ("orders_7d", "orders", "weekly"),
            ("orders_total", "orders", "total"),
            ("conversion_rate_pct", "%", "daily"),
            ("users_active_7d", "users", "weekly"),
            ("models_active", "models", "daily"),
            ("revenue_30d", "₽", "monthly"),
            ("revenue_total", "₽", "total"),
            ("avg_deal_days", "days", "daily"),
            ("repeat_client_rate_pct", "%", "weekly"),
            ("reviews_avg_rating", "stars", "daily"),
            ("conversion_new_to_confirmed_pct", "%", "daily"),
        ]
        for key, unit, period in fields:
            if key in metrics and metrics[key] is not None:
                db.record_metric(nevesty_product_id, key, metrics[key], unit, period)

    def analyze(self, all_metrics: dict) -> dict:
        """Run AI analysis on collected metrics, return structured insights."""
        result = self.think_json(
            "Проанализируй метрики бизнеса и выдай инсайты в JSON формате:\n"
            "{\n"
            '  "health_score": <0-100>,\n'
            '  "top_problem": "...",\n'
            '  "top_opportunity": "...",\n'
            '  "insights": ["insight1", "insight2", ...],\n'
            '  "recommended_focus": "conversion|traffic|product|retention|revenue"\n'
            "}",
            context=all_metrics,
            max_tokens=1024,
        )
        if not result:
            result = {
                "health_score": 50,
                "top_problem": "недостаточно данных для анализа",
                "top_opportunity": "начать отслеживать метрики",
                "insights": ["Система только запустилась, данные накапливаются"],
                "recommended_focus": "traffic",
            }
        return result
