"""
Shared pytest fixtures for Nevesty Models factory tests.
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=False)
def no_llm():
    """Prevent actual LLM calls in unit tests."""
    with patch('factory.agents.base.FactoryAgent.think', return_value="Mock LLM response"):
        yield


@pytest.fixture
def sample_order_data():
    """Sample order data for testing analytics agents."""
    return {
        'total': 50,
        'completed': 35,
        'cancelled': 5,
        'new_orders': 10,
        'avg_budget': 25000
    }
