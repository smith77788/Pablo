"""Tests for factory/db.py — БЛОК 7.3: 40+ pytest tests."""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure factory package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ─── Fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file so tests don't touch the real DB."""
    import factory.db as db
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


# ─── _now() ──────────────────────────────────────────────────────────────────

def test_now_returns_string():
    from factory import db
    result = db._now()
    assert isinstance(result, str)


def test_now_contains_T_separator():
    from factory import db
    result = db._now()
    assert "T" in result


def test_now_parseable_by_datetime():
    from factory import db
    result = db._now()
    # Should parse without error
    parsed = datetime.fromisoformat(result)
    assert isinstance(parsed, datetime)


def test_now_is_utc():
    from factory import db
    result = db._now()
    parsed = datetime.fromisoformat(result)
    # UTC offset should be +00:00
    assert parsed.utcoffset().total_seconds() == 0


def test_now_values_increase_over_calls():
    import time
    from factory import db
    t1 = db._now()
    time.sleep(0.01)
    t2 = db._now()
    assert t2 >= t1


# ─── get_conn() ───────────────────────────────────────────────────────────────

def test_get_conn_returns_connection():
    from factory import db
    conn = db.get_conn()
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_get_conn_sets_row_factory():
    from factory import db
    conn = db.get_conn()
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_get_conn_wal_mode():
    from factory import db
    conn = db.get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


def test_get_conn_can_execute_query():
    from factory import db
    conn = db.get_conn()
    result = conn.execute("SELECT 1 AS val").fetchone()
    assert result["val"] == 1
    conn.close()


# ─── init_db() ────────────────────────────────────────────────────────────────

def _get_tables(tmp_path, monkeypatch):
    """Helper: return set of table names in the test DB."""
    import factory.db as db
    conn = db.get_conn()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_init_db_creates_products_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "products" in tables


def test_init_db_creates_ideas_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "ideas" in tables


def test_init_db_creates_experiments_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "experiments" in tables


def test_init_db_creates_growth_actions_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "growth_actions" in tables


def test_init_db_creates_cycles_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "cycles" in tables


def test_init_db_creates_metrics_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "metrics" in tables


def test_init_db_creates_decisions_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "decisions" in tables


def test_init_db_creates_monthly_reports_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "monthly_reports" in tables


def test_init_db_creates_agent_reports_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "agent_reports" in tables


def test_init_db_creates_ceo_decisions_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "ceo_decisions" in tables


def test_init_db_creates_factory_reports_table():
    from factory import db
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "factory_reports" in tables


def test_init_db_is_idempotent():
    """Calling init_db() twice should not raise any error."""
    from factory import db
    db.init_db()  # second call
    db.init_db()  # third call — still fine


def test_init_db_experiments_has_applied_at_column():
    from factory import db
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(experiments)").fetchall()]
    conn.close()
    assert "applied_at" in cols


def test_init_db_growth_actions_has_description_column():
    from factory import db
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(growth_actions)").fetchall()]
    conn.close()
    assert "description" in cols


def test_init_db_growth_actions_has_outcome_column():
    from factory import db
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(growth_actions)").fetchall()]
    conn.close()
    assert "outcome" in cols


# ─── fetch_one() ─────────────────────────────────────────────────────────────

def test_fetch_one_returns_none_when_no_match():
    from factory import db
    result = db.fetch_one("SELECT * FROM ideas WHERE id=?", (999999,))
    assert result is None


def test_fetch_one_returns_dict_when_found():
    from factory import db
    db.insert("ideas", {
        "title": "Single Idea",
        "description": "desc",
        "created_at": db._now(),
    })
    result = db.fetch_one("SELECT * FROM ideas WHERE title=?", ("Single Idea",))
    assert result is not None
    assert isinstance(result, dict)


def test_fetch_one_returns_correct_value():
    from factory import db
    db.insert("ideas", {
        "title": "CorrectValue",
        "description": "testing value correctness",
        "created_at": db._now(),
    })
    result = db.fetch_one("SELECT * FROM ideas WHERE title=?", ("CorrectValue",))
    assert result["title"] == "CorrectValue"
    assert result["description"] == "testing value correctness"


def test_fetch_one_returns_only_first_row():
    from factory import db
    for i in range(3):
        db.insert("ideas", {"title": "DupTitle", "created_at": db._now()})
    result = db.fetch_one("SELECT * FROM ideas WHERE title=?", ("DupTitle",))
    assert result is not None  # returns exactly one row, not an error


def test_fetch_one_no_params():
    from factory import db
    db.insert("ideas", {"title": "NoParams", "created_at": db._now()})
    result = db.fetch_one("SELECT * FROM ideas")
    assert result is not None


# ─── fetch_all() ─────────────────────────────────────────────────────────────

def test_fetch_all_returns_empty_list_when_no_rows():
    from factory import db
    result = db.fetch_all("SELECT * FROM ideas")
    assert result == []


def test_fetch_all_returns_list():
    from factory import db
    result = db.fetch_all("SELECT * FROM ideas")
    assert isinstance(result, list)


def test_fetch_all_returns_correct_count():
    from factory import db
    for i in range(4):
        db.insert("ideas", {"title": f"Idea {i}", "created_at": db._now()})
    result = db.fetch_all("SELECT * FROM ideas")
    assert len(result) == 4


def test_fetch_all_rows_are_dicts():
    from factory import db
    db.insert("ideas", {"title": "DictCheck", "created_at": db._now()})
    result = db.fetch_all("SELECT * FROM ideas")
    assert all(isinstance(r, dict) for r in result)


def test_fetch_all_with_params():
    from factory import db
    db.insert("ideas", {"title": "FilterMe", "status": "new", "created_at": db._now()})
    db.insert("ideas", {"title": "SkipMe", "status": "rejected", "created_at": db._now()})
    result = db.fetch_all("SELECT * FROM ideas WHERE status=?", ("new",))
    assert len(result) == 1
    assert result[0]["title"] == "FilterMe"


def test_fetch_all_returns_all_columns():
    from factory import db
    db.insert("ideas", {
        "title": "AllCols",
        "description": "testing all columns",
        "category": "saas",
        "priority": 8,
        "status": "new",
        "created_at": db._now(),
    })
    result = db.fetch_all("SELECT * FROM ideas WHERE title=?", ("AllCols",))
    row = result[0]
    assert row["title"] == "AllCols"
    assert row["description"] == "testing all columns"
    assert row["category"] == "saas"
    assert row["priority"] == 8


# ─── execute() ────────────────────────────────────────────────────────────────

def test_execute_insert_row():
    from factory import db
    db.execute(
        "INSERT INTO ideas (title, created_at) VALUES (?, ?)",
        ("ExecInsert", db._now()),
    )
    row = db.fetch_one("SELECT * FROM ideas WHERE title=?", ("ExecInsert",))
    assert row is not None
    assert row["title"] == "ExecInsert"


def test_execute_update_row():
    from factory import db
    row_id = db.insert("ideas", {"title": "BeforeUpdate", "created_at": db._now()})
    db.execute("UPDATE ideas SET title=? WHERE id=?", ("AfterUpdate", row_id))
    row = db.fetch_one("SELECT * FROM ideas WHERE id=?", (row_id,))
    assert row["title"] == "AfterUpdate"


def test_execute_delete_row():
    from factory import db
    row_id = db.insert("ideas", {"title": "ToDelete", "created_at": db._now()})
    db.execute("DELETE FROM ideas WHERE id=?", (row_id,))
    row = db.fetch_one("SELECT * FROM ideas WHERE id=?", (row_id,))
    assert row is None


def test_execute_is_committed():
    """execute() must commit so a fresh connection sees the change."""
    from factory import db
    db.execute(
        "INSERT INTO ideas (title, created_at) VALUES (?, ?)",
        ("CommitCheck", db._now()),
    )
    # Open a brand-new connection to verify persistence
    conn2 = db.get_conn()
    row = conn2.execute("SELECT * FROM ideas WHERE title=?", ("CommitCheck",)).fetchone()
    conn2.close()
    assert row is not None


def test_execute_no_params():
    from factory import db
    db.insert("ideas", {"title": "NoParamExec", "created_at": db._now()})
    # DELETE with no WHERE — deletes all rows; should not raise
    db.execute("DELETE FROM ideas")
    rows = db.fetch_all("SELECT * FROM ideas")
    assert rows == []


# ─── insert() helper ─────────────────────────────────────────────────────────

def test_insert_returns_integer_id():
    from factory import db
    row_id = db.insert("ideas", {"title": "IdReturn", "created_at": db._now()})
    assert isinstance(row_id, int)
    assert row_id > 0


def test_insert_auto_serializes_dict_values():
    from factory import db
    metrics_json = {"key": "val", "num": 42}
    row_id = db.insert("ideas", {
        "title": "JsonInsert",
        "rationale": metrics_json,
        "created_at": db._now(),
    })
    row = db.fetch_one("SELECT * FROM ideas WHERE id=?", (row_id,))
    # The dict should have been stored as a JSON string
    assert json.loads(row["rationale"]) == metrics_json


def test_insert_auto_serializes_list_values():
    from factory import db
    tags = ["saas", "b2b", "ukraine"]
    row_id = db.insert("ideas", {
        "title": "ListInsert",
        "rationale": tags,
        "created_at": db._now(),
    })
    row = db.fetch_one("SELECT * FROM ideas WHERE id=?", (row_id,))
    assert json.loads(row["rationale"]) == tags


# ─── update() helper ──────────────────────────────────────────────────────────

def test_update_sets_updated_at():
    from factory import db
    row_id = db.insert("products", {
        "name": "Prod1",
        "status": "active",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": "2000-01-01T00:00:00+00:00",
    })
    db.update("products", row_id, {"name": "Prod1-Updated"})
    row = db.fetch_one("SELECT * FROM products WHERE id=?", (row_id,))
    assert row["name"] == "Prod1-Updated"
    # updated_at must have been refreshed beyond the original sentinel
    assert row["updated_at"] > "2000-01-01T00:00:00+00:00"


def test_update_serializes_dict():
    from factory import db
    row_id = db.insert("products", {
        "name": "ProdDict",
        "status": "active",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": db._now(),
    })
    payload = {"revenue": 9999}
    db.update("products", row_id, {"success_metrics": payload})
    row = db.fetch_one("SELECT * FROM products WHERE id=?", (row_id,))
    assert json.loads(row["success_metrics"]) == payload


# ─── run alias ───────────────────────────────────────────────────────────────

def test_run_is_alias_for_execute():
    from factory import db
    assert db.run is db.execute


# ─── Domain helpers ──────────────────────────────────────────────────────────

def test_save_and_get_cycle_result():
    from factory import db
    db.save_cycle_result("test-cycle-1", "analytics", {"health_score": 75}, tokens=100)
    row = db.fetch_one("SELECT * FROM cycles WHERE id=?", ("test-cycle-1",))
    assert row is not None
    assert row["phase"] == "analytics"


def test_save_cycle_result_upsert():
    from factory import db
    db.save_cycle_result("cycle-upsert", "analytics", {"health_score": 50})
    db.save_cycle_result("cycle-upsert", "strategic", {"health_score": 70})
    rows = db.fetch_all("SELECT * FROM cycles WHERE id=?", ("cycle-upsert",))
    assert len(rows) == 1
    assert rows[0]["phase"] == "strategic"


def test_save_ceo_decision():
    from factory import db
    db.save_ceo_decision(
        cycle_id="test-cycle-1",
        decision_text="Focus on conversion",
        health_score=80,
        departments_active=["marketing", "product"],
        weekly_focus="conversion",
        department_focus="marketing",
        experiment_proposal={"idea": "A/B test CTAs"},
    )
    recent = db.get_recent_ceo_decisions(limit=1)
    assert len(recent) == 1
    assert recent[0]["cycle_id"] == "test-cycle-1"


def test_get_recent_ceo_decisions_empty():
    from factory import db
    decisions = db.get_recent_ceo_decisions()
    assert isinstance(decisions, list)
    assert len(decisions) == 0


def test_get_recent_ceo_decisions_limit():
    from factory import db
    for i in range(5):
        db.save_ceo_decision(
            cycle_id=f"cycle-{i}",
            decision_text=f"Decision {i}",
            health_score=70 + i,
            departments_active=["marketing"],
        )
    recent = db.get_recent_ceo_decisions(limit=3)
    assert len(recent) == 3


def test_save_factory_report():
    from factory import db
    db.save_factory_report("weekly", "2026-W20", {"summary": "test week"})
    rows = db.fetch_all("SELECT * FROM factory_reports WHERE period_key=?", ("2026-W20",))
    assert len(rows) == 1
    assert rows[0]["report_type"] == "weekly"


def test_save_factory_report_monthly():
    from factory import db
    db.save_factory_report("monthly", "2026-05", {"revenue": 42000})
    rows = db.fetch_all("SELECT * FROM factory_reports WHERE period_key=?", ("2026-05",))
    assert len(rows) == 1
    assert rows[0]["report_type"] == "monthly"


def test_insert_and_fetch_all():
    from factory import db
    db.insert("ideas", {
        "title": "Test Idea",
        "description": "Some description",
        "created_at": "2026-01-01T00:00:00",
    })
    ideas = db.fetch_all("SELECT * FROM ideas WHERE title=?", ("Test Idea",))
    assert len(ideas) == 1
    assert ideas[0]["title"] == "Test Idea"


def test_get_active_products_empty():
    from factory import db
    products = db.get_active_products()
    assert isinstance(products, list)


def test_get_active_products_returns_only_active():
    from factory import db
    db.insert("products", {
        "name": "ActiveProd",
        "status": "active",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": db._now(),
    })
    db.insert("products", {
        "name": "KilledProd",
        "status": "killed",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": db._now(),
    })
    products = db.get_active_products()
    assert len(products) == 1
    assert products[0]["name"] == "ActiveProd"


def test_get_running_experiments_empty():
    from factory import db
    experiments = db.get_running_experiments()
    assert isinstance(experiments, list)


def test_get_running_experiments_filters_concluded():
    from factory import db
    db.insert("experiments", {
        "name": "RunningExp",
        "status": "running",
        "started_at": db._now(),
    })
    db.insert("experiments", {
        "name": "ConcludedExp",
        "status": "concluded",
        "started_at": db._now(),
    })
    exps = db.get_running_experiments()
    assert len(exps) == 1
    assert exps[0]["name"] == "RunningExp"


def test_get_pending_growth_actions_empty():
    from factory import db
    actions = db.get_pending_growth_actions()
    assert isinstance(actions, list)


def test_get_pending_growth_actions_limit():
    from factory import db
    for i in range(5):
        db.insert("growth_actions", {
            "action_type": "seo",
            "channel": "tiktok",
            "status": "pending",
            "priority": i,
            "created_at": db._now(),
        })
    actions = db.get_pending_growth_actions(limit=3)
    assert len(actions) == 3


def test_get_recent_decisions_empty():
    from factory import db
    decisions = db.get_recent_decisions()
    assert isinstance(decisions, list)
    assert len(decisions) == 0


def test_get_recent_decisions_limit():
    from factory import db
    for i in range(5):
        db.insert("decisions", {
            "cycle_id": f"c-{i}",
            "decision_type": "create_mvp",
            "created_at": db._now(),
        })
    decisions = db.get_recent_decisions(limit=2)
    assert len(decisions) == 2


def test_record_metric():
    from factory import db
    product_id = db.insert("products", {
        "name": "TestProduct",
        "status": "active",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": db._now(),
    })
    db.record_metric(product_id, "conversion_rate", 3.5, unit="%", period="daily")
    metrics = db.get_product_metrics(product_id, limit=5)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "conversion_rate"
    assert metrics[0]["value"] == pytest.approx(3.5)


def test_get_product_metrics_empty():
    from factory import db
    metrics = db.get_product_metrics(999999, limit=5)
    assert isinstance(metrics, list)
    assert len(metrics) == 0


def test_get_product_metrics_limit():
    from factory import db
    product_id = db.insert("products", {
        "name": "MetricProd",
        "status": "active",
        "source": "factory",
        "created_at": db._now(),
        "updated_at": db._now(),
    })
    for i in range(5):
        db.record_metric(product_id, "views", float(i * 100))
    metrics = db.get_product_metrics(product_id, limit=3)
    assert len(metrics) == 3
