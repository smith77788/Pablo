"""Database layer — SQLite with async support."""
from __future__ import annotations
import sqlite3
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "factory.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT,
            status      TEXT NOT NULL DEFAULT 'active',  -- active, scaled, killed, paused
            source      TEXT DEFAULT 'factory',          -- factory, manual, imported
            category    TEXT,                             -- saas, marketplace, service, content
            monetization TEXT,
            success_metrics TEXT,                         -- JSON
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            category    TEXT,
            priority    INTEGER DEFAULT 5,
            status      TEXT DEFAULT 'new',  -- new, building, launched, rejected
            rationale   TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experiments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER REFERENCES products(id),
            name        TEXT NOT NULL,
            hypothesis  TEXT,
            variant_a   TEXT,   -- control
            variant_b   TEXT,   -- challenger
            status      TEXT DEFAULT 'running',  -- running, concluded
            result      TEXT,   -- scale, iterate, kill
            conversion_a REAL DEFAULT 0,
            conversion_b REAL DEFAULT 0,
            traffic_split REAL DEFAULT 0.5,
            started_at  TEXT NOT NULL,
            concluded_at TEXT,
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER REFERENCES products(id),
            metric_name TEXT NOT NULL,
            value       REAL NOT NULL,
            unit        TEXT,
            period      TEXT,   -- daily, weekly, monthly
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id    TEXT NOT NULL,
            decision_type TEXT NOT NULL,  -- create_mvp, scale, kill, iterate, grow, experiment
            product_id  INTEGER REFERENCES products(id),
            rationale   TEXT,
            payload     TEXT,   -- JSON with decision details
            executed    INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS growth_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER REFERENCES products(id),
            action_type TEXT,   -- seo, social, ad, ux, cta
            channel     TEXT,   -- tiktok, instagram, seo, telegram, email
            content     TEXT,
            status      TEXT DEFAULT 'pending',  -- pending, live, done, cancelled
            priority    INTEGER DEFAULT 5,
            created_at  TEXT NOT NULL,
            executed_at TEXT,
            experiment_hypothesis TEXT,
            metric_name  TEXT,
            metric_baseline REAL,
            metric_target   REAL,
            metric_current  REAL,
            evaluated_at    TEXT,
            outcome     TEXT DEFAULT 'pending'   -- success, fail, pending, inconclusive
        );

        CREATE TABLE IF NOT EXISTS cycles (
            id          TEXT PRIMARY KEY,  -- ISO timestamp
            phase       TEXT,              -- analytics, strategic, execution, done
            summary     TEXT,
            health_score INTEGER,
            decisions_count INTEGER DEFAULT 0,
            actions_count   INTEGER DEFAULT 0,
            duration_s  REAL,
            started_at  TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS monthly_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            month       TEXT NOT NULL,  -- 'YYYY-MM'
            report_json TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_metrics_product ON metrics(product_id, metric_name);
        CREATE INDEX IF NOT EXISTS idx_metrics_recorded ON metrics(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON decisions(cycle_id);
        CREATE INDEX IF NOT EXISTS idx_growth_status ON growth_actions(status);
        """)
        conn.commit()

        # Migrate: add agent_reports table if missing
        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name  TEXT NOT NULL,
            department  TEXT NOT NULL,
            report_type TEXT NOT NULL,
            summary     TEXT,
            cycle_id    INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        )
        """)
        conn.commit()

        # Migrate: add nevesty_experiments table if missing
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nevesty_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis TEXT NOT NULL,
            metric TEXT NOT NULL,
            baseline REAL,
            target REAL,
            status TEXT DEFAULT 'proposed',
            result TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """)
        conn.commit()

        # Migrate: add ceo_decisions table if missing
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ceo_decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id        TEXT NOT NULL,
            decision_text   TEXT,
            health_score    INTEGER,
            departments_active TEXT,   -- JSON list
            weekly_focus    TEXT,
            department_focus TEXT,
            experiment_proposal TEXT,  -- JSON
            created_at      TEXT NOT NULL
        )
        """)
        conn.commit()

        # Migrate: add factory_reports table if missing (weekly/periodic summaries)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS factory_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,   -- weekly, monthly, experiment_auto_apply
            period_key  TEXT NOT NULL,   -- e.g. '2026-W20', '2026-05'
            report_json TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
        """)
        conn.commit()

        # Migrate existing growth_actions table — add experiment columns if missing
        _existing = [r[1] for r in conn.execute("PRAGMA table_info(growth_actions)").fetchall()]
        _new_cols = {
            "experiment_hypothesis": "TEXT",
            "metric_name":           "TEXT",
            "metric_baseline":       "REAL",
            "metric_target":         "REAL",
            "metric_current":        "REAL",
            "evaluated_at":          "TEXT",
            "outcome":               "TEXT DEFAULT 'pending'",
        }
        for col, col_def in _new_cols.items():
            if col not in _existing:
                conn.execute(f"ALTER TABLE growth_actions ADD COLUMN {col} {col_def}")
        conn.commit()


# ─── Generic CRUD helpers ─────────────────────────────────────────────────────

def insert(table: str, data: dict) -> int:
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(data.values()))
        conn.commit()
        return cur.lastrowid


def update(table: str, row_id: int, data: dict) -> None:
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
    data["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in data)
    with get_conn() as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [row_id])
        conn.commit()


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> None:
    with get_conn() as conn:
        conn.execute(sql, params)
        conn.commit()


# ─── Domain-specific queries ──────────────────────────────────────────────────

def get_active_products() -> list[dict]:
    return fetch_all("SELECT * FROM products WHERE status='active' ORDER BY created_at DESC")


def get_product_metrics(product_id: int, limit: int = 30) -> list[dict]:
    return fetch_all(
        "SELECT * FROM metrics WHERE product_id=? ORDER BY recorded_at DESC LIMIT ?",
        (product_id, limit)
    )


def get_running_experiments() -> list[dict]:
    return fetch_all("SELECT * FROM experiments WHERE status='running' ORDER BY started_at DESC")


def get_recent_decisions(limit: int = 20) -> list[dict]:
    return fetch_all(
        "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
    )


def get_pending_growth_actions(limit: int = 10) -> list[dict]:
    return fetch_all(
        "SELECT * FROM growth_actions WHERE status='pending' ORDER BY priority DESC LIMIT ?",
        (limit,)
    )


def record_metric(product_id: int, name: str, value: float, unit: str = "", period: str = "daily") -> None:
    insert("metrics", {
        "product_id": product_id,
        "metric_name": name,
        "value": value,
        "unit": unit,
        "period": period,
        "recorded_at": _now(),
    })


def get_recent_ceo_decisions(limit: int = 3) -> list[dict]:
    """Return the last N CEO decisions for context."""
    return fetch_all(
        "SELECT decision_text, health_score, weekly_focus, department_focus, "
        "experiment_proposal, created_at FROM ceo_decisions ORDER BY created_at DESC LIMIT ?",
        (limit,)
    )


def save_ceo_decision(
    cycle_id: str,
    decision_text: str,
    health_score: int,
    departments_active: list,
    weekly_focus: str = "",
    department_focus: str = "",
    experiment_proposal: dict | None = None,
) -> int:
    """Persist a CEO decision record."""
    return insert("ceo_decisions", {
        "cycle_id": cycle_id,
        "decision_text": decision_text[:1000] if decision_text else "",
        "health_score": health_score,
        "departments_active": json.dumps(departments_active),
        "weekly_focus": weekly_focus,
        "department_focus": department_focus,
        "experiment_proposal": json.dumps(experiment_proposal or {}),
        "created_at": _now(),
    })
