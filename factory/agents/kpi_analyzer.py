"""KPI analysis for Nevesty Models — analyzes 8 key business metrics."""
from __future__ import annotations
import datetime
import sqlite3
import os


DB_PATH = os.getenv('DB_PATH', '/home/user/Pablo/nevesty-models/db/nevesty.db')


def _connect_db(path: str) -> sqlite3.Connection | None:
    """Open SQLite connection or return None if file doesn't exist."""
    try:
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


class KPIAnalyzer:
    """Analyzes all 8 KPIs from the Nevesty Models business."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path

    def analyze_orders_per_period(self, conn: sqlite3.Connection | None = None) -> dict:
        """KPI 1: Orders count per period (today, week, month, total)."""
        if conn is None:
            return {"today": 0, "week": 0, "month": 0, "total": 0}
        try:
            today = datetime.date.today().isoformat()
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
            month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
            cur = conn.cursor()
            total = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            today_count = cur.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (today,)).fetchone()[0]
            week_count = cur.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (week_ago,)).fetchone()[0]
            month_count = cur.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (month_ago,)).fetchone()[0]
            return {"today": today_count, "week": week_count, "month": month_count, "total": total}
        except Exception:
            return {"today": 0, "week": 0, "month": 0, "total": 0}

    def analyze_conversion_by_source(self, conn: sqlite3.Connection | None = None) -> dict:
        """KPI 2: Conversion rate (new → confirmed/completed)."""
        if conn is None:
            return {"total": 0, "completed": 0, "conversion_rate": 0.0}
        try:
            cur = conn.cursor()
            total = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            completed = cur.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN ('confirmed','completed')"
            ).fetchone()[0]
            rate = round(completed / total, 3) if total > 0 else 0.0
            return {"total": total, "completed": completed, "conversion_rate": rate}
        except Exception:
            return {"total": 0, "completed": 0, "conversion_rate": 0.0}

    def analyze_popular_categories(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        """KPI 3: Popular model categories by order count."""
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT m.category, COUNT(o.id) as order_count
                FROM orders o
                JOIN models m ON o.model_id = m.id
                WHERE m.category IS NOT NULL
                GROUP BY m.category
                ORDER BY order_count DESC
            """).fetchall()
            return [{"category": r[0], "order_count": r[1]} for r in rows]
        except Exception:
            return []

    def analyze_client_return_rate(self, conn: sqlite3.Connection | None = None) -> dict:
        """KPI 4: Client return rate — % of clients with 2+ orders."""
        if conn is None:
            return {"total_clients": 0, "returning_clients": 0, "return_rate": 0.0}
        try:
            cur = conn.cursor()
            total_clients = cur.execute(
                "SELECT COUNT(DISTINCT client_chat_id) FROM orders"
            ).fetchone()[0]
            returning = cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT client_chat_id FROM orders
                    GROUP BY client_chat_id HAVING COUNT(*) >= 2
                )
            """).fetchone()[0]
            rate = round(returning / total_clients, 3) if total_clients > 0 else 0.0
            return {"total_clients": total_clients, "returning_clients": returning, "return_rate": rate}
        except Exception:
            return {"total_clients": 0, "returning_clients": 0, "return_rate": 0.0}

    def analyze_deal_cycle_days(self, conn: sqlite3.Connection | None = None) -> dict:
        """KPI 5: Average deal cycle (new → completed) in days."""
        if conn is None:
            return {"avg_days": 0, "min_days": 0, "max_days": 0, "sample_size": 0}
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT julianday(updated_at) - julianday(created_at) as days
                FROM orders
                WHERE status = 'completed' AND updated_at IS NOT NULL AND created_at IS NOT NULL
            """).fetchall()
            days_list = [r[0] for r in rows if r[0] is not None and r[0] >= 0]
            if not days_list:
                return {"avg_days": 0, "min_days": 0, "max_days": 0, "sample_size": 0}
            return {
                "avg_days": round(sum(days_list) / len(days_list), 1),
                "min_days": round(min(days_list), 1),
                "max_days": round(max(days_list), 1),
                "sample_size": len(days_list),
            }
        except Exception:
            return {"avg_days": 0, "min_days": 0, "max_days": 0, "sample_size": 0}

    def analyze_model_ratings(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        """KPI 6: Model ratings from approved reviews."""
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT m.name, m.id, AVG(r.rating) as avg_rating, COUNT(r.id) as review_count
                FROM reviews r
                JOIN models m ON r.model_id = m.id
                WHERE r.approved = 1
                GROUP BY m.id
                ORDER BY avg_rating DESC
                LIMIT 10
            """).fetchall()
            return [
                {"name": r[0], "model_id": r[1], "avg_rating": round(r[2], 2), "review_count": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def analyze_bot_activity(self, conn: sqlite3.Connection | None = None) -> dict:
        """KPI 7: Bot activity — unique users, active sessions."""
        if conn is None:
            return {"unique_users": 0, "active_sessions": 0, "users_this_week": 0}
        try:
            cur = conn.cursor()
            unique_users = cur.execute(
                "SELECT COUNT(DISTINCT client_chat_id) FROM orders"
            ).fetchone()[0]
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
            users_week = cur.execute(
                "SELECT COUNT(DISTINCT client_chat_id) FROM orders WHERE created_at >= ?", (week_ago,)
            ).fetchone()[0]
            # Count active telegram sessions if table exists
            try:
                active_sessions = cur.execute(
                    "SELECT COUNT(*) FROM telegram_sessions WHERE updated_at > datetime('now', '-1 hour')"
                ).fetchone()[0]
            except Exception:
                active_sessions = 0
            return {"unique_users": unique_users, "active_sessions": active_sessions, "users_this_week": users_week}
        except Exception:
            return {"unique_users": 0, "active_sessions": 0, "users_this_week": 0}

    def analyze_top_client_requests(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        """KPI 8: Top client requests by event type and category."""
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT event_type, COUNT(*) as count
                FROM orders
                WHERE event_type IS NOT NULL
                GROUP BY event_type
                ORDER BY count DESC
                LIMIT 5
            """).fetchall()
            return [{"event_type": r[0], "count": r[1]} for r in rows]
        except Exception:
            return []

    def run_full_analysis(self) -> dict:
        """Run all 8 KPI analyses and return combined report."""
        conn = _connect_db(self.db_path)
        try:
            result = {
                "timestamp": datetime.datetime.now().isoformat(),
                "kpi_1_orders_per_period": self.analyze_orders_per_period(conn),
                "kpi_2_conversion": self.analyze_conversion_by_source(conn),
                "kpi_3_popular_categories": self.analyze_popular_categories(conn),
                "kpi_4_client_return_rate": self.analyze_client_return_rate(conn),
                "kpi_5_deal_cycle_days": self.analyze_deal_cycle_days(conn),
                "kpi_6_model_ratings": self.analyze_model_ratings(conn),
                "kpi_7_bot_activity": self.analyze_bot_activity(conn),
                "kpi_8_top_requests": self.analyze_top_client_requests(conn),
            }
        finally:
            if conn:
                conn.close()
        return result

    def generate_summary(self, analysis: dict | None = None) -> str:
        """Generate a human-readable KPI summary."""
        a = analysis or self.run_full_analysis()
        lines = [
            "📊 *KPI АНАЛИЗ*\n",
            f"📦 Заявок (всего): {a['kpi_1_orders_per_period'].get('total', 0)}",
            f"📦 За месяц: {a['kpi_1_orders_per_period'].get('month', 0)}",
            f"✅ Конверсия: {a['kpi_2_conversion'].get('conversion_rate', 0)*100:.1f}%",
            f"🔄 Возврат клиентов: {a['kpi_4_client_return_rate'].get('return_rate', 0)*100:.1f}%",
            f"⏱ Средний цикл сделки: {a['kpi_5_deal_cycle_days'].get('avg_days', 0)} дн.",
            f"👥 Уникальных клиентов: {a['kpi_7_bot_activity'].get('unique_users', 0)}",
        ]
        return "\n".join(lines)
