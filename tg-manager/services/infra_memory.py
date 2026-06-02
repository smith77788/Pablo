"""
Infrastructure Memory — системная память о производительности ресурсов.

Отслеживает в реальном времени:
  - Какие аккаунты успешно выполняли какие операции
  - Паттерны ошибок по аккаунтам/прокси/типам операций
  - Паттерны времени суток (когда операции успешнее)
  - Качество прокси по типам операций

Хранит состояние in-memory (как flood_engine) + персистирует в БД через schema_v65.
Интегрируется с resource_selector для принятия умных решений о выборе ресурсов.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# ── In-memory хранилище ───────────────────────────────────────────────────────

@dataclass
class _AccountActionRecord:
    """История операций одного аккаунта по одному типу действия."""
    account_id: int
    action_type: str
    successes: int = 0
    failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_errors: list[str] = field(default_factory=list)  # последние 5 ошибок
    hour_successes: dict[int, int] = field(default_factory=dict)  # час → число успехов

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.5  # нейтральный по умолчанию
        return self.successes / self.total

    @property
    def memory_score(self) -> float:
        """Score от 0 (плохой) до 1 (отличный) на основе истории."""
        rate = self.success_rate
        # Вес опыта: чем больше операций, тем точнее оценка
        confidence = min(1.0, self.total / 20.0)
        # Смещение к 0.5 при малом опыте (shrinkage к нейтральному)
        return rate * confidence + 0.5 * (1 - confidence)


@dataclass
class _ProxyRecord:
    """История качества прокси по типам операций."""
    proxy_url: str
    action_type: str
    successes: int = 0
    failures: int = 0
    avg_latency_ms: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return self.successes / total if total > 0 else 0.5


# Global in-memory state
# (account_id, action_type) -> _AccountActionRecord
_account_memory: dict[tuple[int, str], _AccountActionRecord] = {}

# (proxy_url, action_type) -> _ProxyRecord
_proxy_memory: dict[tuple[str, str], _ProxyRecord] = {}

# Dirty set для отложенной записи в БД
_dirty_account_keys: set[tuple[int, str]] = set()
_dirty_proxy_keys: set[tuple[str, str]] = set()

# Фоновая задача для записи в БД
_flush_task: Optional[asyncio.Task] = None
_FLUSH_INTERVAL = 60  # секунд между записью в БД


# ── Запись событий ────────────────────────────────────────────────────────────

def record_account_op(
    account_id: int,
    action_type: str,
    success: bool,
    error: Optional[str] = None,
) -> None:
    """Записать результат операции для аккаунта (in-memory, non-blocking).

    Вызывается из account_manager, strike_engine, op_worker после каждой операции.
    """
    key = (account_id, action_type)
    if key not in _account_memory:
        _account_memory[key] = _AccountActionRecord(
            account_id=account_id, action_type=action_type
        )
    rec = _account_memory[key]
    now = time.time()

    if success:
        rec.successes += 1
        rec.last_success_at = now
        # Трекинг по часу суток
        hour = int(time.strftime("%H", time.localtime(now)))
        rec.hour_successes[hour] = rec.hour_successes.get(hour, 0) + 1
    else:
        rec.failures += 1
        rec.last_failure_at = now
        if error:
            # Хранить последние 5 ошибок
            rec.last_errors = (rec.last_errors + [error[:100]])[-5:]

    _dirty_account_keys.add(key)


def record_proxy_op(
    proxy_url: str,
    action_type: str,
    success: bool,
    latency_ms: float = 0.0,
) -> None:
    """Записать результат операции для прокси (in-memory, non-blocking)."""
    if not proxy_url:
        return
    key = (proxy_url, action_type)
    if key not in _proxy_memory:
        _proxy_memory[key] = _ProxyRecord(proxy_url=proxy_url, action_type=action_type)
    rec = _proxy_memory[key]
    now = time.time()

    if success:
        rec.successes += 1
        rec.last_success_at = now
        if latency_ms > 0:
            # Скользящее среднее задержки
            n = rec.successes
            rec.avg_latency_ms = rec.avg_latency_ms * (n - 1) / n + latency_ms / n
    else:
        rec.failures += 1
        rec.last_failure_at = now

    _dirty_proxy_keys.add(key)


# ── Запросы памяти ────────────────────────────────────────────────────────────

def get_account_score(account_id: int, action_type: str) -> float:
    """Получить memory_score аккаунта для данного типа действия.

    Возвращает float от 0 до 1 (0.5 = нейтральный/новый).
    """
    key = (account_id, action_type)
    if key not in _account_memory:
        # Также проверить "default" action_type как fallback
        key_default = (account_id, "default")
        if key_default in _account_memory:
            return _account_memory[key_default].memory_score
        return 0.5  # нейтральный
    return _account_memory[key].memory_score


def get_proxy_score(proxy_url: str, action_type: str) -> float:
    """Получить success_rate прокси для данного типа действия."""
    if not proxy_url:
        return 0.5
    key = (proxy_url, action_type)
    if key not in _proxy_memory:
        return 0.5
    return _proxy_memory[key].success_rate


def get_best_hour(account_id: int, action_type: str) -> Optional[int]:
    """Вернуть час суток с наибольшим числом успехов для аккаунта, или None."""
    key = (account_id, action_type)
    if key not in _account_memory:
        return None
    hrs = _account_memory[key].hour_successes
    if not hrs:
        return None
    return max(hrs, key=hrs.__getitem__)


def get_account_summary(account_id: int, action_type: str = "default") -> dict:
    """Получить сводку по аккаунту для данного action_type."""
    key = (account_id, action_type)
    if key not in _account_memory:
        return {
            "account_id": account_id,
            "action_type": action_type,
            "total": 0,
            "successes": 0,
            "failures": 0,
            "success_rate": 0.5,
            "memory_score": 0.5,
            "last_errors": [],
            "best_hour": None,
        }
    rec = _account_memory[key]
    return {
        "account_id": account_id,
        "action_type": action_type,
        "total": rec.total,
        "successes": rec.successes,
        "failures": rec.failures,
        "success_rate": round(rec.success_rate, 3),
        "memory_score": round(rec.memory_score, 3),
        "last_errors": rec.last_errors,
        "best_hour": get_best_hour(account_id, action_type),
    }


def rank_accounts_by_memory(
    account_ids: list[int],
    action_type: str,
) -> list[tuple[int, float]]:
    """Отсортировать аккаунты по memory_score для данного action_type.

    Возвращает list of (account_id, memory_score), убывающий порядок.
    """
    scored = [
        (acc_id, get_account_score(acc_id, action_type))
        for acc_id in account_ids
    ]
    return sorted(scored, key=lambda x: x[1], reverse=True)


def get_error_patterns(account_id: int) -> dict[str, list[str]]:
    """Получить паттерны ошибок аккаунта по всем типам операций."""
    result = {}
    for (acc_id, action_type), rec in _account_memory.items():
        if acc_id == account_id and rec.last_errors:
            result[action_type] = rec.last_errors
    return result


# ── Персистентность в БД ──────────────────────────────────────────────────────

async def flush_to_db(pool: asyncpg.Pool) -> None:
    """Записать dirty-записи в БД. Вызывается фоновой задачей каждые 60 секунд."""
    # Аккаунты
    dirty_accounts = list(_dirty_account_keys)
    _dirty_account_keys.clear()

    for key in dirty_accounts:
        rec = _account_memory.get(key)
        if not rec:
            continue
        try:
            await pool.execute(
                """INSERT INTO infra_memory_accounts
                       (account_id, action_type, successes, failures,
                        last_success_at, last_failure_at, last_errors, updated_at)
                   VALUES ($1, $2, $3, $4,
                       to_timestamp($5), to_timestamp($6),
                       $7, NOW())
                   ON CONFLICT (account_id, action_type)
                   DO UPDATE SET
                       successes = infra_memory_accounts.successes + EXCLUDED.successes,
                       failures = infra_memory_accounts.failures + EXCLUDED.failures,
                       last_success_at = GREATEST(infra_memory_accounts.last_success_at, EXCLUDED.last_success_at),
                       last_failure_at = GREATEST(infra_memory_accounts.last_failure_at, EXCLUDED.last_failure_at),
                       last_errors = EXCLUDED.last_errors,
                       updated_at = NOW()""",
                rec.account_id, rec.action_type,
                rec.successes, rec.failures,
                rec.last_success_at if rec.last_success_at else None,
                rec.last_failure_at if rec.last_failure_at else None,
                rec.last_errors,
            )
        except Exception as e:
            log.warning("infra_memory flush account %s/%s: %s", key[0], key[1], e)
            _dirty_account_keys.add(key)  # вернуть в dirty для повтора

    # Прокси
    dirty_proxies = list(_dirty_proxy_keys)
    _dirty_proxy_keys.clear()

    for key in dirty_proxies:
        rec = _proxy_memory.get(key)
        if not rec:
            continue
        try:
            await pool.execute(
                """INSERT INTO infra_memory_proxies
                       (proxy_url, action_type, successes, failures,
                        avg_latency_ms, last_success_at, last_failure_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5,
                       to_timestamp($6), to_timestamp($7), NOW())
                   ON CONFLICT (proxy_url, action_type)
                   DO UPDATE SET
                       successes = infra_memory_proxies.successes + EXCLUDED.successes,
                       failures = infra_memory_proxies.failures + EXCLUDED.failures,
                       avg_latency_ms = EXCLUDED.avg_latency_ms,
                       last_success_at = GREATEST(infra_memory_proxies.last_success_at, EXCLUDED.last_success_at),
                       last_failure_at = GREATEST(infra_memory_proxies.last_failure_at, EXCLUDED.last_failure_at),
                       updated_at = NOW()""",
                rec.proxy_url, rec.action_type,
                rec.successes, rec.failures,
                rec.avg_latency_ms,
                rec.last_success_at if rec.last_success_at else None,
                rec.last_failure_at if rec.last_failure_at else None,
            )
        except Exception as e:
            log.warning("infra_memory flush proxy %s/%s: %s", key[0], key[1], e)
            _dirty_proxy_keys.add(key)

    if dirty_accounts or dirty_proxies:
        log.debug(
            "infra_memory flush: %d account records, %d proxy records",
            len(dirty_accounts), len(dirty_proxies),
        )


async def load_from_db(pool: asyncpg.Pool, owner_id: int) -> None:
    """Загрузить историю из БД при старте (для восстановления после рестарта).

    Загружает только активные аккаунты владельца, не перезаписывает in-memory данные
    если они уже накоплены.
    """
    try:
        rows = await pool.fetch(
            """SELECT ima.account_id, ima.action_type,
                      ima.successes, ima.failures,
                      EXTRACT(EPOCH FROM ima.last_success_at) as last_success_ts,
                      EXTRACT(EPOCH FROM ima.last_failure_at) as last_failure_ts,
                      ima.last_errors
               FROM infra_memory_accounts ima
               JOIN tg_accounts a ON a.id = ima.account_id
               WHERE a.owner_id = $1 AND a.is_active = TRUE""",
            owner_id,
        )
        loaded = 0
        for row in rows:
            key = (row["account_id"], row["action_type"])
            if key in _account_memory:
                # Не перетирать свежие in-memory данные устаревшими из БД
                continue
            rec = _AccountActionRecord(
                account_id=row["account_id"],
                action_type=row["action_type"],
                successes=row["successes"] or 0,
                failures=row["failures"] or 0,
                last_success_at=float(row["last_success_ts"] or 0),
                last_failure_at=float(row["last_failure_ts"] or 0),
                last_errors=list(row["last_errors"] or []),
            )
            _account_memory[key] = rec
            loaded += 1

        log.info("infra_memory: loaded %d account records for owner=%d", loaded, owner_id)
    except Exception as e:
        log.warning("infra_memory load_from_db failed for owner=%d: %s", owner_id, e)


async def run_flush_loop(pool: asyncpg.Pool) -> None:
    """Фоновый цикл: периодически записывает in-memory данные в БД."""
    log.info("infra_memory: flush loop started (interval=%ds)", _FLUSH_INTERVAL)
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            if _dirty_account_keys or _dirty_proxy_keys:
                await flush_to_db(pool)
        except asyncio.CancelledError:
            # Финальный flush перед остановкой
            try:
                await flush_to_db(pool)
            except Exception:
                pass
            raise
        except Exception as e:
            log.warning("infra_memory flush loop error: %s", e)


# ── Отчёты ────────────────────────────────────────────────────────────────────

def format_account_report(account_ids: list[int], action_type: str) -> str:
    """Форматированный отчёт о памяти аккаунтов для Telegram."""
    if not account_ids:
        return "Нет данных"

    ranked = rank_accounts_by_memory(account_ids, action_type)
    lines = [f"📊 <b>Infrastructure Memory</b> [{action_type}]\n"]

    for acc_id, score in ranked[:10]:
        key = (acc_id, action_type)
        if key in _account_memory:
            rec = _account_memory[key]
            bar = "█" * round(score * 5) + "░" * (5 - round(score * 5))
            lines.append(
                f"acc:{acc_id} [{bar}] {score:.0%} "
                f"({rec.successes}✅/{rec.failures}❌)"
            )
        else:
            lines.append(f"acc:{acc_id} [░░░░░] нет данных")

    return "\n".join(lines)
