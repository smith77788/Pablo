import pytest


def test_import_op_worker():
    from services import op_worker
    assert hasattr(op_worker, '_circuit_breaker_record')
    assert hasattr(op_worker, 'get_adaptive_delay')


def test_import_account_manager():
    from services import account_manager
    assert hasattr(account_manager, 'test_proxy')
    assert hasattr(account_manager, 'auto_select_proxy')


def test_import_account_warmer():
    from services import account_warmer
    assert hasattr(account_warmer, 'run_warmup_loop')


def test_import_strike_engine():
    from services import strike_engine
    assert hasattr(strike_engine, 'get_randomized_interval')
    assert hasattr(strike_engine, 'is_strike_allowed')


def test_import_ecosystem_brain():
    from services import ecosystem_brain
    assert hasattr(ecosystem_brain, 'discover_channel_relationships')
    assert hasattr(ecosystem_brain, 'get_ecosystem_recommendations')


def test_import_resource_selector():
    from services import resource_selector
    assert hasattr(resource_selector, 'select_account')
    assert hasattr(resource_selector, 'select_account_rotated')


def test_import_flood_engine():
    from services import flood_engine
    assert hasattr(flood_engine, 'get_best_account')


def test_import_rate_limiter():
    """Ограничитель запросов живёт в security и подключён middleware'ом."""
    from services.security import check_rate_limit, rate_limit_response
    assert callable(check_rate_limit) and callable(rate_limit_response)
