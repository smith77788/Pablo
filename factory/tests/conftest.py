"""
Shared pytest fixtures for Nevesty Models factory tests.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Ensure factory package is importable when running tests from any directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=False)
def no_llm():
    """Prevent actual LLM calls in unit tests."""
    with patch('factory.agents.base.FactoryAgent.think', return_value="Mock LLM response"):
        yield


@pytest.fixture
def mock_anthropic():
    """Mock Anthropic API to avoid real API calls in tests."""
    with patch('anthropic.Anthropic') as mock:
        instance = MagicMock()
        mock.return_value = instance
        instance.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"result": "test output"}')]
        )
        yield instance


@pytest.fixture
def sample_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    import sqlite3
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE models (
            id INTEGER PRIMARY KEY, name TEXT, age INTEGER, height INTEGER,
            city TEXT, category TEXT, available INTEGER DEFAULT 1,
            featured INTEGER DEFAULT 0, bio TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, model_id INTEGER, status TEXT DEFAULT 'new',
            budget REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO models (name, age, height, city, category) VALUES (?, ?, ?, ?, ?)",
        ('Test Model', 25, 175, 'Москва', 'fashion'),
    )
    conn.execute(
        "INSERT INTO orders (model_id, status, budget) VALUES (?, ?, ?)",
        (1, 'completed', 50000),
    )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def sample_order_data():
    """Sample order data for testing analytics agents."""
    return {
        'total': 50,
        'completed': 35,
        'cancelled': 5,
        'new_orders': 10,
        'avg_budget': 25000,
    }
