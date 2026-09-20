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
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
from services.logger import log_exc_swallow

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
    avg_duration_s: float = (
        0.0  # скользящее среднее времени выполнения (сек на элемент)
    )
    duration_samples: int = 0  # количество измерений duration
    # Сколько успехов/провалов уже УЛЕТЕЛО в БД. Нужно, чтобы флашить ПРИРОСТ,
    # а не абсолют: при двух репликах перезапись абсолютом («successes =
    # EXCLUDED.successes») затирает инкременты соседа. В том же запросе метки
    # времени сливались через GREATEST — счётчики были единственным местом,
    # где слияния не было.
    flushed_successes: int = 0
    flushed_failures: int = 0

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
    flushed_successes: int = 0      # см. одноимённые поля у _AccountActionRecord
    flushed_failures: int = 0

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
    duration_s: Optional[float] = None,
) -> None:
    """Записать результат операции для аккаунта (in-memory, non-blocking).

    duration_s — время выполнения одного элемента в секундах (для обучения Prediction Engine).
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
        # Скользящее среднее времени выполнения (только успешные)
        if duration_s is not None and duration_s > 0:
            n = rec.duration_samples + 1
            rec.avg_duration_s = rec.avg_duration_s * (n - 1) / n + duration_s / n
            rec.duration_samples = n
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
    """Записать результат операции для прокси (in-memory, non-blocking).

    Ключ нормализуется к PLAINTEXT: proxy_url может прийти зашифрованным из
    user_proxies (недетерминированный шифр) — без нормализации ключи бы «поплыли»
    (разный шифротекст для одного прокси) и PK infra_memory_proxies размножился бы.
    """
    if not proxy_url:
        return
    from services.token_vault import decrypt_token

    proxy_url = decrypt_token(proxy_url)
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
    """Получить success_rate прокси для данного типа действия.

    Ключ нормализуется к plaintext (симметрично record_proxy_op): на вход может
    прийти зашифрованный proxy_url из user_proxies — иначе lookup промахнётся.
    """
    if not proxy_url:
        return 0.5
    from services.token_vault import decrypt_token

    key = (decrypt_token(proxy_url), action_type)
    if key not in _proxy_memory:
        return 0.5
    return _proxy_memory[key].success_rate


def get_proxy_summary(proxy_url: str) -> Optional[dict]:
    """Сводная статистика прокси по всем типам действий (реальные операции).

    Возвращает {success, fail, total, success_rate, avg_latency_ms, last_check}
    или None, если по прокси ещё нет наблюдений. Используется админ-панелью,
    чтобы показывать РЕАЛЬНОЕ качество прокси, а не только результаты test_proxy.

    Ключ нормализуется к plaintext (симметрично record_proxy_op/get_proxy_score):
    на вход может прийти зашифрованный proxy_url из user_proxies.
    """
    if not proxy_url:
        return None
    from services.token_vault import decrypt_token

    proxy_url = decrypt_token(proxy_url)
    recs = [r for (url, _), r in _proxy_memory.items() if url == proxy_url]
    if not recs:
        return None
    success = sum(r.successes for r in recs)
    fail = sum(r.failures for r in recs)
    total = success + fail
    if total == 0:
        return None
    lat_recs = [r for r in recs if r.avg_latency_ms > 0 and r.successes > 0]
    avg_latency = (
        int(sum(r.avg_latency_ms * r.successes for r in lat_recs)
            / sum(r.successes for r in lat_recs))
        if lat_recs else 0
    )
    last_check = max(
        (max(r.last_success_at, r.last_failure_at) for r in recs), default=0.0
    )
    return {
        "success": success,
        "fail": fail,
        "total": total,
        "success_rate": round(success / total * 100, 1),
        "avg_latency_ms": avg_latency,
        "last_check": last_check,
    }


def get_account_avg_duration(account_id: int, action_type: str) -> Optional[float]:
    """Вернуть среднее время выполнения одного элемента (сек), или None если нет данных.

    Используется Prediction Engine для обучения на реальных временах вместо статичных констант.
    """
    key = (account_id, action_type)
    if key not in _account_memory:
        return None
    rec = _account_memory[key]
    if rec.duration_samples < 3:
        return None  # недостаточно данных
    return rec.avg_duration_s


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
        (acc_id, get_account_score(acc_id, action_type)) for acc_id in account_ids
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
                        last_success_at, last_failure_at, last_errors,
                        avg_duration_s, updated_at)
                   VALUES ($1, $2, $3, $4,
                       to_timestamp($5), to_timestamp($6),
                       $7, $8, NOW())
                   ON CONFLICT (account_id, action_type)
                   DO UPDATE SET
                       -- ПРИБАВЛЯЕМ прирост, а не перезаписываем абсолютом:
                       -- иначе вторая реплика затирает инкременты первой.
                       -- $3/$4 здесь — дельта с прошлого флаша.
                       successes = infra_memory_accounts.successes + EXCLUDED.successes,
                       failures = infra_memory_accounts.failures + EXCLUDED.failures,
                       last_success_at = GREATEST(infra_memory_accounts.last_success_at, EXCLUDED.last_success_at),
                       last_failure_at = GREATEST(infra_memory_accounts.last_failure_at, EXCLUDED.last_failure_at),
                       last_errors = EXCLUDED.last_errors,
                       avg_duration_s = CASE
                           WHEN EXCLUDED.avg_duration_s > 0 THEN EXCLUDED.avg_duration_s
                           ELSE infra_memory_accounts.avg_duration_s
                       END,
                       updated_at = NOW()""",
                rec.account_id,
                rec.action_type,
                max(0, rec.successes - rec.flushed_successes),
                max(0, rec.failures - rec.flushed_failures),
                rec.last_success_at if rec.last_success_at > 0 else None,
                rec.last_failure_at if rec.last_failure_at > 0 else None,
                rec.last_errors,
                rec.avg_duration_s if rec.avg_duration_s > 0 else 0.0,
            )
            # Прирост записан — фиксируем отметку. БЕЗ этого следующий флаш
            # прибавил бы те же значения ещё раз и счётчики бы раздувались.
            rec.flushed_successes = rec.successes
            rec.flushed_failures = rec.failures
        except Exception as e:
            log.warning("infra_memory flush account %s/%s: %s", key[0], key[1], e)
            _dirty_account_keys.add(key)  # вернуть в dirty для повтора
            # Отметку НЕ двигаем: при повторе прирост уйдёт целиком.

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
                       -- Прирост, а не абсолют (см. infra_memory_accounts).
                       successes = infra_memory_proxies.successes + EXCLUDED.successes,
                       failures = infra_memory_proxies.failures + EXCLUDED.failures,
                       avg_latency_ms = EXCLUDED.avg_latency_ms,
                       last_success_at = GREATEST(infra_memory_proxies.last_success_at, EXCLUDED.last_success_at),
                       last_failure_at = GREATEST(infra_memory_proxies.last_failure_at, EXCLUDED.last_failure_at),
                       updated_at = NOW()""",
                rec.proxy_url,
                rec.action_type,
                max(0, rec.successes - rec.flushed_successes),
                max(0, rec.failures - rec.flushed_failures),
                rec.avg_latency_ms,
                rec.last_success_at if rec.last_success_at > 0 else None,
                rec.last_failure_at if rec.last_failure_at > 0 else None,
            )
            rec.flushed_successes = rec.successes      # см. account-ветку выше
            rec.flushed_failures = rec.failures
        except Exception as e:
            log.warning("infra_memory flush proxy %s/%s: %s", key[0], key[1], e)
            _dirty_proxy_keys.add(key)

    if dirty_accounts or dirty_proxies:
        log.debug(
            "infra_memory flush: %d account records, %d proxy records",
            len(dirty_accounts),
            len(dirty_proxies),
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
                      ima.last_errors,
                      COALESCE(ima.avg_duration_s, 0) AS avg_duration_s
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
            successes = row["successes"] or 0
            failures = row["failures"] or 0
            avg_dur = float(row["avg_duration_s"] or 0)
            rec = _AccountActionRecord(
                account_id=row["account_id"],
                action_type=row["action_type"],
                successes=successes,
                failures=failures,
                last_success_at=float(row["last_success_ts"] or 0),
                last_failure_at=float(row["last_failure_ts"] or 0),
                last_errors=list(row["last_errors"] or []),
                avg_duration_s=avg_dur,
                # duration только для успешных; если avg есть — инициализируем как successes
                duration_samples=successes if avg_dur > 0 else 0,
                # Загруженное из БД уже ТАМ записано. Без этой отметки первый же
                # флаш прибавил бы весь абсолют поверх — счётчики бы удвоились.
                flushed_successes=successes,
                flushed_failures=failures,
            )
            _account_memory[key] = rec
            loaded += 1

        log.info(
            "infra_memory: loaded %d account records for owner=%d", loaded, owner_id
        )
    except Exception as e:
        log.warning("infra_memory load_from_db failed for owner=%d: %s", owner_id, e)


async def load_all_from_db(pool: asyncpg.Pool) -> None:
    """Загрузить всю историю из БД при старте без фильтрации по owner_id.

    Вызывается один раз из run_flush_loop перед первым sleep,
    чтобы восстановить learned patterns после рестарта бота.
    Не перезаписывает уже накопленные in-memory данные.
    """
    # --- аккаунты ---
    try:
        rows = await pool.fetch(
            """SELECT account_id, action_type,
                      successes, failures,
                      EXTRACT(EPOCH FROM last_success_at) as last_success_ts,
                      EXTRACT(EPOCH FROM last_failure_at) as last_failure_ts,
                      last_errors,
                      COALESCE(avg_duration_s, 0) AS avg_duration_s
               FROM infra_memory_accounts
               WHERE successes + failures > 0"""
        )
        loaded = 0
        for row in rows:
            key = (row["account_id"], row["action_type"])
            if key in _account_memory:
                continue  # не перетирать свежие in-memory данные
            successes = row["successes"] or 0
            avg_dur = float(row["avg_duration_s"] or 0)
            rec = _AccountActionRecord(
                account_id=row["account_id"],
                action_type=row["action_type"],
                successes=successes,
                failures=row["failures"] or 0,
                last_success_at=float(row["last_success_ts"] or 0),
                last_failure_at=float(row["last_failure_ts"] or 0),
                last_errors=list(row["last_errors"] or []),
                avg_duration_s=avg_dur,
                duration_samples=successes if avg_dur > 0 else 0,
                # Загруженное уже в БД — без отметки первый флаш прибавил бы
                # абсолют поверх себя и счётчики удвоились бы на каждом рестарте.
                flushed_successes=successes,
                flushed_failures=row["failures"] or 0,
            )
            _account_memory[key] = rec
            loaded += 1
        log.info("infra_memory: loaded %d account records from DB on startup", loaded)
    except Exception as e:
        # Таблица может не существовать до первой миграции — это нормально
        log.info("infra_memory: load_all_from_db accounts skipped (%s)", type(e).__name__)

    # --- прокси ---
    try:
        proxy_rows = await pool.fetch(
            """SELECT proxy_url, action_type,
                      successes, failures,
                      avg_latency_ms,
                      EXTRACT(EPOCH FROM last_success_at) as last_success_ts,
                      EXTRACT(EPOCH FROM last_failure_at) as last_failure_ts
               FROM infra_memory_proxies
               WHERE successes + failures > 0"""
        )
        loaded_proxies = 0
        for row in proxy_rows:
            key = (row["proxy_url"], row["action_type"])
            if key in _proxy_memory:
                continue  # не перетирать свежие in-memory данные
            rec = _ProxyRecord(
                proxy_url=row["proxy_url"],
                action_type=row["action_type"],
                successes=row["successes"] or 0,
                failures=row["failures"] or 0,
                avg_latency_ms=float(row["avg_latency_ms"] or 0),
                last_success_at=float(row["last_success_ts"] or 0),
                last_failure_at=float(row["last_failure_ts"] or 0),
                # Загруженное уже в БД — иначе первый флаш удвоит счётчики.
                flushed_successes=row["successes"] or 0,
                flushed_failures=row["failures"] or 0,
            )
            _proxy_memory[key] = rec
            loaded_proxies += 1
        if loaded_proxies:
            log.info(
                "infra_memory: loaded %d proxy records from DB on startup", loaded_proxies
            )
    except Exception as e:
        log.info("infra_memory: load_all_from_db proxies skipped (%s)", type(e).__name__)


async def run_flush_loop(pool: asyncpg.Pool) -> None:
    """Фоновый цикл: загружает историю при старте, затем периодически пишет dirty-данные в БД."""
    log.info("infra_memory: flush loop started (interval=%ds)", _FLUSH_INTERVAL)
    # Один раз — загрузить всё при старте
    await load_all_from_db(pool)
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            if _dirty_account_keys or _dirty_proxy_keys:
                await flush_to_db(pool)
        except asyncio.CancelledError:
            # Финальный flush перед остановкой
            try:
                await flush_to_db(pool)
            except Exception as e:
                log_exc_swallow(log, "run_flush_loop: flush_to_db")
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
                f"acc:{acc_id} [{bar}] {score:.0%} ({rec.successes}✅/{rec.failures}❌)"
            )
        else:
            lines.append(f"acc:{acc_id} [░░░░░] нет данных")

    return "\n".join(lines)


# ── Единый риск-пульс аккаунта (Волна S/1B: кровоток иммунных сигналов) ────────
# Сводит РАЗРОЗНЕННЫЕ иммунные сигналы (restriction_events, account_flood_log)
# в один «пульс», который читают нервная (op_worker) и кровеносная (account_manager)
# системы ПЕРЕД действием. Продюсеры не меняются — агрегируем на чтении поверх
# уже существующих таблиц. Всё DB-based (в отличие от in-memory кэша выше).

async def is_account_quarantined(pool, account_id: int, *, days: int = 3) -> bool:
    """Fail-open: True только при недавнем СЕРЬЁЗНОМ ограничении (critical severity
    или event_type про ban/block/restrict) за последние `days` суток.

    Fail-open — принципиально: любая ошибка/отсутствие сигнала → False (НЕ блокируем).
    Anti-detection ядро не должно вставать из-за отсутствия данных пульса —
    карантин только при явно подтверждённом ограничении.
    """
    if not pool or not account_id:
        return False
    try:
        # risk_cleared_at — владелец вручную снял риск («взять в работу»): считаем
        # только ограничения ПОЗЖЕ этой отметки, иначе ручной сброс не работал бы
        # (кнопка чистит кулдаун, а карантин по старым событиям держал аккаунт вне
        # операций — жалоба «кулдаун не сбрасывается»). Событие new-severity
        # свежее сброса снова уводит аккаунт в карантин — это правильно.
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM restriction_events re "
            "JOIN tg_accounts a ON a.id = re.account_id "
            "WHERE re.account_id=$1 "
            "AND re.created_at > NOW() - make_interval(days => $2) "
            "AND (a.risk_cleared_at IS NULL OR re.created_at > a.risk_cleared_at) "
            "AND (re.severity='critical' OR re.event_type ILIKE '%ban%' "
            "OR re.event_type ILIKE '%block%' OR re.event_type ILIKE '%restrict%')",
            account_id, days)
        return bool(n and n > 0)
    except Exception:
        return False


async def get_account_health(pool, owner_id: int, *, days: int = 7) -> dict:
    """Риск-пульс всех активных аккаунтов владельца — единый сигнал здоровья.

    Агрегирует поверх иммунных таблиц (read-only, продюсеры не трогаем):
      - restriction_events: critical/ban/block/restrict → карантин; warning → риск;
      - account_flood_log: частые флуд-ожидания → риск.
    score 0..1 (1 = здоров). status: quarantine|at_risk|healthy.
    Возвращает {accounts:[...], summary:{healthy, at_risk, quarantine, total}}.
    """
    empty = {"accounts": [], "summary": {"healthy": 0, "at_risk": 0,
                                         "quarantine": 0, "total": 0}}
    if not pool or not owner_id:
        return empty
    try:
        rows = await pool.fetch(
            """
            SELECT a.id, a.phone, COALESCE(a.acc_status,'active') AS acc_status,
              a.trust_score,
              -- истёк ли кулдаун: acc_status='cooldown' с прошедшим окном НЕ риск
              -- (само-heal в account_monitor чистит статус, но здесь окно закрываем
              -- сразу, чтобы UI не держал «Под риском» до следующего цикла монитора)
              (a.cooldown_until IS NOT NULL AND a.cooldown_until > NOW()) AS cd_active,
              (SELECT COUNT(*) FROM restriction_events r
                 WHERE r.account_id=a.id
                   AND r.created_at > NOW() - make_interval(days => $2)) AS restrictions,
              (SELECT COUNT(*) FROM restriction_events r
                 WHERE r.account_id=a.id
                   AND r.created_at > NOW() - make_interval(days => $2)
                   AND (r.severity='critical' OR r.event_type ILIKE '%ban%'
                        OR r.event_type ILIKE '%block%'
                        OR r.event_type ILIKE '%restrict%')) AS severe,
              (SELECT COUNT(*) FROM account_flood_log f
                 WHERE f.account_id=a.id
                   AND f.created_at > NOW() - make_interval(days => $2)) AS floods
            FROM tg_accounts a
            WHERE a.owner_id=$1 AND a.is_active=TRUE
            ORDER BY a.id
            """, owner_id, days)
    except Exception as e:
        log.warning("get_account_health owner=%s: %s", owner_id, e)
        return empty

    accounts = []
    healthy = at_risk = quarantine = 0
    for r in rows:
        severe = int(r["severe"] or 0)
        restr = int(r["restrictions"] or 0)
        floods = int(r["floods"] or 0)
        acc_status = (r["acc_status"] or "active").lower()
        # acc_status тоже сигнал здоровья (его ставят warmer/recovery/op_worker):
        # banned/session_expired → карантин; cooldown/warming → риск.
        status_bad = acc_status in ("banned", "session_expired", "deleted")
        status_risk = acc_status in ("warming", "restricted", "flood")
        # 'cooldown' — риск ТОЛЬКО пока окно кулдауна не истекло (иначе давний
        # FloodWait держал бы «Под риском» бесконечно до heal-цикла монитора).
        if acc_status == "cooldown" and r.get("cd_active"):
            status_risk = True
        # trust_score (0..1, ставится trust_engine): низкий траст — тоже риск.
        # Единый пульс сводит restriction_events + acc_status + flood + trust.
        try:
            trust = float(r["trust_score"]) if r["trust_score"] is not None else 1.0
        except (TypeError, ValueError):
            trust = 1.0
        low_trust = trust < 0.4
        # Последний разрозненный орган — in-memory account_health.health_score
        # (0..100, у неизвестных = 100). Сводим и его в единый пульс: <10 = мёртв
        # (карантин), <30 = риск. process-local (может быть пустым на свежем воркере
        # → 100 = нейтрально, никогда не флагает ложно).
        try:
            from services import account_health as _ah
            hscore = float(_ah.get_health(r["id"]).health_score)
        except Exception:
            hscore = 100.0
        if severe or status_bad or hscore < 10.0:
            status, score = "quarantine", 0.2
            quarantine += 1
        elif restr or floods >= 3 or status_risk or low_trust or hscore < 30.0:
            status, score = "at_risk", min(0.5, trust)
            at_risk += 1
        elif floods:
            status, score = "at_risk", 0.7
            at_risk += 1
        else:
            status, score = "healthy", round(min(1.0, 0.7 + trust * 0.3), 3)
            healthy += 1
        accounts.append({
            "account_id": r["id"], "phone": r["phone"],
            "status": status, "score": score, "acc_status": acc_status,
            "trust_score": round(trust, 3),
            "restrictions": restr, "floods": floods,
        })
    # худшие — вперёд (для приборного щитка)
    accounts.sort(key=lambda a: a["score"])
    return {"accounts": accounts,
            "summary": {"healthy": healthy, "at_risk": at_risk,
                        "quarantine": quarantine, "total": len(accounts)}}
