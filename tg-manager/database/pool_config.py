"""Database Connection Pool Configuration — environment-tuned asyncpg pool settings."""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)


# Границы потолка обращения к БД. Снизу — чтобы настройка не выключала защиту
# (см. комментарий у `command_timeout`), сверху — чтобы «очень большое» не
# означало то же бесконечное ожидание. Миграции живут в этом же пуле и под
# текущим потолком работают, поэтому запас сверху им ничего не ломает.
_CMD_TIMEOUT_MIN = 5.0
_CMD_TIMEOUT_MAX = 600.0


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
        # ЗАЖИМ, А НЕ ПРОСТО ЧТЕНИЕ. Это единственный потолок на ВСЕ обращения
        # к БД, и снять его значит включить неизлечимый класс потери операции:
        # повисший запрос (обычно — ожидание блокировки) не вернётся в цикл
        # исполнителя, поэтому потолок прогона не сработает, а оба сторожа
        # зависших операцию пощадят — она числится активной в памяти процесса.
        # Операция остаётся 'running' навсегда и лечится только перезапуском.
        # Ноль в asyncpg безобиден наравне с отсутствием таймаута, поэтому
        # `DB_CMD_TIMEOUT=0` читался бы как «без потолка»: опечатка в среде не
        # вправе этого делать. Верхняя граница — чтобы «очень большое» не
        # оказалось тем же бесконечным ожиданием под видом настройки.
        "command_timeout": _float_env(
            "DB_CMD_TIMEOUT", 30, minimum=_CMD_TIMEOUT_MIN,
            maximum=_CMD_TIMEOUT_MAX),
        "statement_cache_size": _int_env("DB_STATEMENT_CACHE_SIZE", 100),
    }


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _float_env(
    key: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Прочитать число из среды, при желании зажав его в границы.

    Границы нужны там, где настройка выключает защитный механизм: мусор в
    переменной отлавливает `except`, а осмысленный, но опасный ноль — нет.
    """
    try:
        value = float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        value = default
    if minimum is not None and value < minimum:
        log.warning(
            "%s=%s вне допустимых границ — взято %s", key, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning(
            "%s=%s вне допустимых границ — взято %s", key, value, maximum)
        value = maximum
    return value


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
