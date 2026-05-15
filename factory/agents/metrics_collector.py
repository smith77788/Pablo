"""Metrics collector: reads real data from Nevesty Models SQLite database."""

import os
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path


class MetricsCollector:
    """Collects key business metrics from the Nevesty Models database."""

    def __init__(self):
        # Find the nevesty DB — prefer env var, then common locations
        db_path_env = os.environ.get('NEVESTY_DB_PATH') or os.environ.get('DB_PATH', '')
        if db_path_env and db_path_env != ':memory:':
            self.db_path = Path(db_path_env)
        else:
            factory_dir = Path(__file__).parent.parent
            candidates = [
                factory_dir.parent / 'nevesty-models' / 'data.db',
                factory_dir.parent / 'nevesty-models' / 'data' / 'nevesty.db',
                factory_dir.parent / 'nevesty-models' / 'nevesty.db',
                factory_dir / 'nevesty.db',
                Path('/home/user/Pablo/nevesty-models/data.db'),
            ]
            self.db_path = next((p for p in candidates if p.exists()), None)

    def _connect(self):
        """Open a read-only connection to nevesty DB."""
        if not self.db_path or not self.db_path.exists():
            return None
        try:
            conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            return None

    def collect_all(self) -> dict:
        """Collect all metrics. Returns dict with all KPIs."""
        conn = self._connect()
        if not conn:
            return self._empty_metrics()
        try:
            metrics = {}
            metrics.update(self._orders_metrics(conn))
            metrics.update(self._models_metrics(conn))
            metrics.update(self._clients_metrics(conn))
            metrics.update(self._revenue_metrics(conn))
            metrics.update(self._engagement_metrics(conn))
            metrics['collected_at'] = datetime.utcnow().isoformat()
            metrics['db_available'] = True
            return metrics
        except Exception as e:
            return {**self._empty_metrics(), 'error': str(e)}
        finally:
            conn.close()

    def _orders_metrics(self, conn) -> dict:
        """Order-related metrics."""
        now = datetime.utcnow()
        today = now.strftime('%Y-%m-%d')
        week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        month_ago = (now - timedelta(days=30)).strftime('%Y-%m-%d')

        def q(sql, *args):
            row = conn.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else 0

        total = q("SELECT COUNT(*) FROM orders")
        confirmed_completed = q(
            "SELECT COUNT(*) FROM orders WHERE status IN ('confirmed','completed','paid','in_progress')"
        )
        non_new = q("SELECT COUNT(*) FROM orders WHERE status != 'new'")

        return {
            'orders_total': total,
            'orders_today': q("SELECT COUNT(*) FROM orders WHERE DATE(created_at) = ?", today),
            'orders_week': q("SELECT COUNT(*) FROM orders WHERE created_at >= ?", week_ago),
            'orders_month': q("SELECT COUNT(*) FROM orders WHERE created_at >= ?", month_ago),
            'orders_new': q("SELECT COUNT(*) FROM orders WHERE status='new'"),
            'orders_confirmed': q("SELECT COUNT(*) FROM orders WHERE status='confirmed'"),
            'orders_completed': q("SELECT COUNT(*) FROM orders WHERE status='completed'"),
            'orders_cancelled': q("SELECT COUNT(*) FROM orders WHERE status='cancelled'"),
            'conversion_rate': round(
                confirmed_completed / max(non_new, 1) * 100, 1
            ),
        }

    def _models_metrics(self, conn) -> dict:
        def q(sql, *args):
            row = conn.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else 0

        # Top models by order_count or by JOIN with orders
        try:
            top_models_rows = conn.execute(
                """SELECT name, order_count as cnt
                   FROM models
                   WHERE (archived=0 OR archived IS NULL)
                   ORDER BY order_count DESC LIMIT 3"""
            ).fetchall()
            top_models = [dict(r) for r in top_models_rows] if top_models_rows else []
        except Exception:
            top_models = []

        return {
            'models_total': q("SELECT COUNT(*) FROM models WHERE available=1 AND (archived=0 OR archived IS NULL)"),
            'models_featured': q("SELECT COUNT(*) FROM models WHERE featured=1 AND available=1 AND (archived=0 OR archived IS NULL)"),
            'models_archived': q("SELECT COUNT(*) FROM models WHERE archived=1"),
            'top_models': top_models,
        }

    def _clients_metrics(self, conn) -> dict:
        def q(sql, *args):
            row = conn.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else 0

        month_ago = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')
        unique = q("SELECT COUNT(DISTINCT client_phone) FROM orders WHERE client_phone IS NOT NULL AND client_phone != ''")
        repeat = q(
            "SELECT COUNT(*) FROM ("
            "SELECT client_phone FROM orders WHERE client_phone IS NOT NULL AND client_phone != '' "
            "GROUP BY client_phone HAVING COUNT(*) > 1"
            ")"
        )
        return {
            'clients_unique': unique,
            'clients_repeat': repeat,
            'clients_new_month': q(
                "SELECT COUNT(DISTINCT client_phone) FROM orders "
                "WHERE created_at >= ? AND client_phone IS NOT NULL AND client_phone != ''",
                month_ago
            ),
            'avg_orders_per_client': round(
                q("SELECT COUNT(*) FROM orders") / max(unique, 1), 2
            ),
        }

    def _revenue_metrics(self, conn) -> dict:
        """Revenue from budget field (stored as text like '50000' or '50000₽')."""
        def parse_budget_sql(status_clause):
            """Sum budget field after stripping non-numeric chars."""
            try:
                row = conn.execute(
                    f"SELECT COALESCE(SUM("
                    f"  CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)"
                    f"), 0) FROM orders WHERE {status_clause} AND budget IS NOT NULL AND budget != ''"
                ).fetchone()
                return int(row[0] or 0)
            except Exception:
                return 0

        month_ago = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')

        revenue_total = parse_budget_sql("status IN ('completed','paid')")
        revenue_month = parse_budget_sql(
            f"status IN ('completed','paid') AND created_at >= '{month_ago}'"
        )

        # Average check on confirmed/completed orders with budget
        try:
            avg_row = conn.execute(
                "SELECT COALESCE(AVG("
                "  CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)"
                "), 0) FROM orders "
                "WHERE status IN ('confirmed','completed','paid','in_progress') "
                "AND budget IS NOT NULL AND budget != '' AND budget != '0'"
            ).fetchone()
            avg_check = round(avg_row[0] or 0)
        except Exception:
            avg_check = 0

        pipeline_value = parse_budget_sql("status IN ('new','confirmed') AND budget != '0'")

        return {
            'revenue_total': revenue_total,
            'revenue_month': revenue_month,
            'avg_check': avg_check,
            'pipeline_value': pipeline_value,
        }

    def _engagement_metrics(self, conn) -> dict:
        def q(sql, *args):
            row = conn.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else 0

        return {
            'reviews_total': q("SELECT COUNT(*) FROM reviews"),
            'reviews_approved': q("SELECT COUNT(*) FROM reviews WHERE approved=1"),
            'avg_rating': round(q("SELECT COALESCE(AVG(rating), 0) FROM reviews WHERE approved=1"), 2),
            'bot_users_total': (
                q("SELECT COUNT(DISTINCT chat_id) FROM telegram_sessions")
                if self._table_exists(conn, 'telegram_sessions') else 0
            ),
        }

    def _table_exists(self, conn, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def _empty_metrics(self) -> dict:
        return {
            'db_available': False,
            'orders_total': 0,
            'orders_today': 0,
            'orders_week': 0,
            'orders_month': 0,
            'orders_new': 0,
            'orders_confirmed': 0,
            'orders_completed': 0,
            'orders_cancelled': 0,
            'conversion_rate': 0,
            'models_total': 0,
            'models_featured': 0,
            'models_archived': 0,
            'clients_unique': 0,
            'clients_repeat': 0,
            'clients_new_month': 0,
            'avg_orders_per_client': 0,
            'revenue_total': 0,
            'revenue_month': 0,
            'avg_check': 0,
            'pipeline_value': 0,
            'reviews_total': 0,
            'reviews_approved': 0,
            'avg_rating': 0,
            'bot_users_total': 0,
            'top_models': [],
            'collected_at': datetime.utcnow().isoformat(),
        }
