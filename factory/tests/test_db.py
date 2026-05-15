"""Tests for factory/db.py"""
import os
import sys
import pytest
import tempfile
from pathlib import Path

# Ensure factory package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file so tests don't touch the real DB."""
    import factory.db as db
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def test_save_and_get_cycle_result():
    from factory import db
    db.save_cycle_result("test-cycle-1", "analytics", {"health_score": 75}, tokens=100)
    # Should not raise; verify the row exists
    row = db.fetch_one("SELECT * FROM cycles WHERE id=?", ("test-cycle-1",))
    assert row is not None
    assert row["phase"] == "analytics"


def test_save_cycle_result_upsert():
    """save_cycle_result should update an existing cycle row."""
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


def test_fetch_one_returns_none_when_missing():
    from factory import db
    result = db.fetch_one("SELECT * FROM ideas WHERE id=?", (999999,))
    assert result is None


def test_get_active_products_empty():
    from factory import db
    products = db.get_active_products()
    assert isinstance(products, list)


def test_get_running_experiments_empty():
    from factory import db
    experiments = db.get_running_experiments()
    assert isinstance(experiments, list)


def test_get_pending_growth_actions_empty():
    from factory import db
    actions = db.get_pending_growth_actions()
    assert isinstance(actions, list)


def test_record_metric():
    from factory import db
    # Insert a product first to satisfy FK (SQLite doesn't enforce FK by default)
    product_id = db.insert("products", {
        "name": "TestProduct",
        "status": "active",
        "source": "factory",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    })
    db.record_metric(product_id, "conversion_rate", 3.5, unit="%", period="daily")
    metrics = db.get_product_metrics(product_id, limit=5)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "conversion_rate"
    assert metrics[0]["value"] == pytest.approx(3.5)
