"""Database Optimization — query analysis, index suggestions, slow query detection."""

from __future__ import annotations

import time
import logging
import re
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


# Slow query threshold in seconds
_SLOW_QUERY_THRESHOLD = 1.0


class QueryTracker:
    """Track query execution times for optimization."""
    
    def __init__(self):
        self._queries: dict[str, dict] = {}
        self._slow_queries: list[dict] = []
    
    def record(self, query: str, duration_s: float, rows_affected: int = 0) -> None:
        """Record a query execution."""
        # Normalize query (remove values for grouping)
        normalized = self._normalize_query(query)
        
        stats = self._queries.setdefault(normalized, {
            "count": 0,
            "total_time": 0,
            "max_time": 0,
            "rows_affected": 0,
        })
        stats["count"] += 1
        stats["total_time"] += duration_s
        stats["max_time"] = max(stats["max_time"], duration_s)
        stats["rows_affected"] += rows_affected
        
        # Track slow queries
        if duration_s > _SLOW_QUERY_THRESHOLD:
            self._slow_queries.append({
                "query": query[:200],
                "duration_s": duration_s,
                "rows": rows_affected,
            })
            # Keep last 100 slow queries
            if len(self._slow_queries) > 100:
                self._slow_queries = self._slow_queries[-100:]
    
    def get_stats(self, top_n: int = 10) -> list[dict]:
        """Get top queries by total time."""
        sorted_queries = sorted(
            self._queries.items(),
            key=lambda x: x[1]["total_time"],
            reverse=True,
        )
        return [
            {
                "query": q[:100],
                "count": s["count"],
                "total_time": round(s["total_time"], 2),
                "avg_time": round(s["total_time"] / s["count"], 3),
                "max_time": round(s["max_time"], 3),
            }
            for q, s in sorted_queries[:top_n]
        ]
    
    def get_slow_queries(self, limit: int = 20) -> list[dict]:
        """Get recent slow queries."""
        return self._slow_queries[-limit:]
    
    def _normalize_query(self, query: str) -> str:
        """Normalize query for grouping (remove specific values)."""
        # Replace $1, $2, etc with ?
        normalized = re.sub(r'\$\d+', '?', query)
        # Replace string literals
        normalized = re.sub(r"'[^']*'", "'?'", normalized)
        # Replace numbers
        normalized = re.sub(r'\b\d+\b', '?', normalized)
        # Normalize whitespace
        normalized = ' '.join(normalized.split())
        return normalized[:200]


# Global query tracker
query_tracker = QueryTracker()


async def analyze_table_usage(pool: asyncpg.Pool, table_name: str) -> dict:
    """Analyze table usage and suggest indexes."""
    try:
        # Get table size
        size = await pool.fetchval(
            "SELECT pg_size_pretty(pg_total_relation_size($1))",
            table_name,
        )
        
        # Get row count
        row_count = await pool.fetchval(f"SELECT COUNT(*) FROM {table_name}")
        
        # Get existing indexes
        indexes = await pool.fetch(
            """SELECT indexname, indexdef
               FROM pg_indexes
               WHERE tablename=$1""",
            table_name,
        )
        
        # Get column info
        columns = await pool.fetch(
            """SELECT column_name, data_type, is_nullable
               FROM information_schema.columns
               WHERE table_name=$1
               ORDER BY ordinal_position""",
            table_name,
        )
        
        return {
            "table": table_name,
            "size": size,
            "row_count": row_count,
            "indexes": [{"name": i["indexname"], "def": i["indexdef"]} for i in indexes],
            "columns": [{"name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"] == "YES"} for c in columns],
        }
    except Exception as e:
        log.warning("analyze_table_usage failed for %s: %s", table_name, e)
        return {"table": table_name, "error": str(e)}


async def suggest_indexes(pool: asyncpg.Pool, table_name: str) -> list[dict]:
    """Suggest indexes based on query patterns."""
    suggestions = []
    
    try:
        # Check for common patterns
        columns = await pool.fetch(
            """SELECT column_name, data_type
               FROM information_schema.columns
               WHERE table_name=$1""",
            table_name,
        )
        
        col_names = {c["column_name"] for c in columns}
        
        # Common index patterns
        if "owner_id" in col_names:
            suggestions.append({
                "column": "owner_id",
                "reason": "Foreign key — most queries filter by owner",
                "sql": f"CREATE INDEX IF NOT EXISTS idx_{table_name}_owner ON {table_name}(owner_id)",
            })
        
        if "created_at" in col_names:
            suggestions.append({
                "column": "created_at",
                "reason": "Timestamp — useful for range queries and sorting",
                "sql": f"CREATE INDEX IF NOT EXISTS idx_{table_name}_created ON {table_name}(created_at DESC)",
            })
        
        if "status" in col_names:
            suggestions.append({
                "column": "status",
                "reason": "Status — used for filtering active/pending items",
                "sql": f"CREATE INDEX IF NOT EXISTS idx_{table_name}_status ON {table_name}(status) WHERE status != 'done'",
            })
        
        if "account_id" in col_names:
            suggestions.append({
                "column": "account_id",
                "reason": "Foreign key — operations linked to accounts",
                "sql": f"CREATE INDEX IF NOT EXISTS idx_{table_name}_account ON {table_name}(account_id)",
            })
        
    except Exception as e:
        log.warning("suggest_indexes failed for %s: %s", table_name, e)
    
    return suggestions
