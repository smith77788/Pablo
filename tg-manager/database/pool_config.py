"""Database Connection Pool Configuration — environment-tuned asyncpg pool settings."""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)


def get_pool_config() -> dict:
    """Build asyncpg pool configuration from environment variables with sensible defaults.

    Environment variables (all optional):
        DB_POOL_MIN:        Minimum pool connections (default: 5)
        DB_POOL_MAX:        Maximum pool connections (default: 20)
        DB_POOL_MAX_IDLE:   Max inactive connection lifetime in seconds (default: 300)
        DB_CMD_TIMEOUT:     Query command timeout in seconds (default: 30)
        DB_STATEMENT_CACHE_SIZE: Prepared statement cache size (default: 100)
    """
    return {
        "min_size": _int_env("DB_POOL_MIN", 5),
        "max_size": _int_env("DB_POOL_MAX", 20),
        "max_inactive_connection_lifetime": _float_env("DB_POOL_MAX_IDLE", 300),
        "command_timeout": _float_env("DB_CMD_TIMEOUT", 30),
        "statement_cache_size": _int_env("DB_STATEMENT_CACHE_SIZE", 100),
    }


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def log_pool_config(config: dict) -> None:
    """Log pool configuration at startup for observability."""
    log.info(
        "DB pool config: min=%d max=%d max_idle=%ds cmd_timeout=%ds cache=%d",
        config["min_size"],
        config["max_size"],
        config["max_inactive_connection_lifetime"],
        config["command_timeout"],
        config["statement_cache_size"],
    )
