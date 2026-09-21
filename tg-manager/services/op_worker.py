"""Фоновый воркер для выполнения очереди операций (параллельный режим)."""

import asyncio
import json
import logging
import os
import random
import re
import socket
import time
import uuid
import aiohttp
import asyncpg
from aiogram import Bot
from database import db
from services.logger import log_exc_swallow
from services.error_codes import ErrorCode
from services.error_reporting import report_error
from bot.utils.op_helpers import extract_flood_wait
from services import resource_selector
from services import infra_memory as _infra_mem
from services import session_simulator
from services import geo_tempo
from services.pacing_engine import get_pacing_engine
from services.secret_masking import mask_bot_token, redact_secrets

log = logging.getLogger(__name__)

# Autopost v2: op_type'ы, которым разрешена рекуррентная переочередь (постинг в
# СВОИ каналы/ботов). Аккаунт-действия сюда НЕ входят — чтобы не зациклить.
_RECURRING_OK_OPS = frozenset({
    "quick_post", "mass_publish", "bulk_post_chans", "bulk_post_to_channel",
    "network_broadcast", "run_broadcast", "group_announce", "pin_last_post",
})


# ── Safe DB helpers ──────────────────────────────────────────────────────────
# All DB calls in the worker must be wrapped to prevent a single query failure
# from crashing the entire worker loop or losing operation state.

async def _safe_execute(pool: asyncpg.Pool, query: str, *args, log_ctx: str = "") -> str:
    """Execute a query, return result or 'ERROR' on failure. Never raises."""
    try:
        return await pool.execute(query, *args)
    except Exception as e:
        report_error(e, extra={"context": f"op_worker_execute{log_ctx}", "query": query[:100]})
        log.warning("op_worker DB execute failed%s: %s", log_ctx, e)
        return "ERROR"


async def _safe_fetchrow(pool: asyncpg.Pool, query: str, *args, log_ctx: str = "") -> asyncpg.Record | None:
    """Fetch a single row, return None on failure. Never raises."""
    try:
        return await pool.fetchrow(query, *args)
    except Exception as e:
        report_error(e, extra={"context": f"op_worker_fetchrow{log_ctx}"})
        log.warning("op_worker DB fetchrow failed%s: %s", log_ctx, e)
        return None


async def _safe_fetch(pool: asyncpg.Pool, query: str, *args, log_ctx: str = "") -> list[asyncpg.Record]:
    """Fetch multiple rows, return empty list on failure. Never raises."""
    try:
        return await pool.fetch(query, *args)
    except Exception as e:
        report_error(e, extra={"context": f"op_worker_fetch{log_ctx}"})
        log.warning("op_worker DB fetch failed%s: %s", log_ctx, e)
        return []


async def _safe_fetchval(pool: asyncpg.Pool, query: str, *args, log_ctx: str = ""):
    """Fetch a single value, return None on failure. Never raises."""
    try:
        return await pool.fetchval(query, *args)
    except Exception as e:
        report_error(e, extra={"context": f"op_worker_fetchval{log_ctx}"})
        log.warning("op_worker DB fetchval failed%s: %s", log_ctx, e)
        return None


_POLL_INTERVAL = 10  # секунд между проверками очереди
_STALE_RUNNING_TIMEOUT_MIN = 60  # операции в running > N минут → reset в pending

def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """Целое из окружения с зажимом в разумные границы (мусор → default)."""
    try:
        return max(lo, min(int(os.getenv(name, "").strip() or default), hi))
    except (TypeError, ValueError):
        return default


# Потолок параллельных операций ЭТОГО процесса (находка аудита №2).
# Раньше 8 было жёстко зашито и означало ёмкость ВСЕЙ платформы: все клиенты
# делили восемь слотов, и добавить мощности было нечем. Теперь это настройка
# воркера — подняв её или запустив вторую реплику воркера (ROLE=worker), ёмкость
# растёт. Дефолт прежний, чтобы поведение без настройки не изменилось.
_MAX_PARALLEL = _int_env("OP_MAX_PARALLEL", 8, 1, 256)
# Максимум на одного владельца — чтобы один клиент не занял весь воркер.
_MAX_PARALLEL_PER_OWNER = _int_env("OP_MAX_PARALLEL_PER_OWNER", 3, 1, 64)
_MIN_ACCOUNTS_PER_OP = 1     # честный дележ флота: минимум аккаунтов на операцию

# Сколько раз операцию можно воскресить после падения/зависания ВОРКЕРА, прежде
# чем признать её ядовитой. Бюджет отдельный от retry_count: тот тратится на
# повторы по ошибке исполнителя (FloodWait, сеть), и смешивать их нельзя —
# иначе честный повтор после флуда съедал бы живучесть, а ядовитая операция
# пряталась бы за неизрасходованным бюджетом флуда.
#
# Без этого предела операция, которая роняет или вешает сам процесс, воскресала
# БЕСКОНЕЧНО: упала → рестарт → снова подхвачена → снова уронила. Каждый круг
# держал слот параллельности и забирал аккаунты флота, а снаружи это выглядело
# как операция, которая «всегда выполняется».
_MAX_REVIVES = _int_env("OP_MAX_REVIVES", 3, 1, 20)

# Потолок одного прогона операции. Без него исполнитель, зависший на ответе
# Telegram (сеть легла, прокси молчит, вызов без своего timeout), держал слот
# параллельности и арендованные аккаунты БЕСКОНЕЧНО — и был при этом невидим:
# сторож зависших пропускает операции, числящиеся активными в этом процессе
# (_active_op_ids), и алерт о застрявших — по тому же критерию. Лечилось только
# рестартом контейнера.
#
# Дефолт намеренно щедрый: массовым операциям положено идти часами (пейсинг
# против банов), и убить здоровую работу хуже, чем поздно поймать зависшую.
# Типам, которые выбиваются из общего потолка, ставится свой `timeout_sec` в
# OP_REGISTRY (services/operation_bus.timeout_for).
#
# Таймаут классифицируется как временная ошибка (_RETRYABLE_ERRORS), поэтому
# уходит в обычный повтор с backoff и, исчерпав max_retries, честно падает в
# failed — отдельной ветки не нужно.
_OP_TIMEOUT_DEFAULT_S = _int_env("OP_TIMEOUT_SEC", 6 * 3600, 5 * 60, 48 * 3600)

_POISON_ERROR = (
    "Операция {n} раз(а) прерывалась вместе с воркером и не дошла до конца — "
    "остановлена, чтобы не занимать флот бесконечно. Запустите её заново."
)


async def _ensure_revive_column(pool: "asyncpg.Pool") -> None:
    """Досоздать operation_queue.revive_count, если миграция ещё не доехала.

    create_pool применяет schema_v218, но деплой и миграции расходятся во
    времени; без колонки ОБА сторожа зависших операций падали бы на каждом
    тике, то есть живучесть исчезала целиком вместо того, чтобы ограничиться.
    """
    try:
        await pool.execute(
            "ALTER TABLE operation_queue "
            "ADD COLUMN IF NOT EXISTS revive_count INT NOT NULL DEFAULT 0"
        )
    except Exception as e:
        log_exc_swallow(log, f"op_worker: ensure revive_count column failed: {e}")



# Реестр аккаунтов, занятых активными операциями op_worker.
# account_warmer проверяет этот реестр перед использованием аккаунта.
#
# ВАЖНО о роли этого set. Он — БЫСТРЫЙ ЛОКАЛЬНЫЙ фильтр, а НЕ арбитр. Настоящий
# арбитр — аренда в БД (см. _db_claim): только она видна другим репликам. Пока
# процесс один, локального set достаточно; со второй репликой он видит своё
# пустое множество и без БД пустил бы одну сессию в два процесса
# (AUTH_KEY_DUPLICATED). Поэтому захват = локальный фильтр И аренда в БД.
_accounts_in_use: set[int] = set()
_operation_account_locks: dict[int, set[int]] = {}
_accounts_lock = asyncio.Lock()

# Опциональный пул БД для персистентных обновлений in_operation.
# Устанавливается через init_op_worker_pool() при старте.
_db_pool: "asyncpg.Pool | None" = None

# ── Аренда аккаунта (межпроцессный арбитр) ───────────────────────────────────
# Идентификатор ЭТОЙ реплики. Пишется в op_lease_owner, чтобы:
#   • освобождать только своё (реплика не должна снимать защиту с чужой сессии);
#   • продлевать только своё (heartbeat);
#   • на старте чистить залипшее, не трогая живые аренды соседей.
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
# Аренда живёт 15 минут и продлевается heartbeat'ом каждый цикл воркера (10 с).
# Упал процесс — аренда истекает сама, и аккаунты возвращаются в оборот без
# 60-минутного сторожа.
_LEASE_TTL_S = 900


def init_op_worker_pool(pool: "asyncpg.Pool") -> None:
    """Вызывать один раз при старте, чтобы mark/release синхронизировались с БД."""
    global _db_pool
    _db_pool = pool
    # Предохранитель живёт в своём модуле, но общее состояние держит в той же БД —
    # прокидываем ему пул (иначе он молча работал бы только на кэше в памяти).
    from services import op_circuit_breaker as _ocb
    _ocb.set_pool(pool)


async def _db_claim(acc_ids: list[int]) -> list[int]:
    """Атомарно взять аренду на аккаунты. Возвращает РЕАЛЬНО захваченные id.

    Единственный межпроцессный арбитр. Один UPDATE ... RETURNING: Postgres
    сериализует конкурирующие обновления строки и перепроверяет WHERE по
    свежей версии, поэтому две реплики физически не могут получить один id.

    Забрать можно свободный аккаунт, аккаунт с истёкшей арендой и СВОЙ
    собственный (идемпотентный повтор). Чужую живую аренду — нельзя.

    FAIL-CLOSED: сбой БД → пустой список. Взять сессию без арбитража опаснее,
    чем не взять: цена ошибки — мёртвый auth-key, а не задержка операции.
    """
    if not acc_ids:
        return []
    if not _db_pool:
        # Нет пула (тесты, одиночный процесс без БД) — решает локальный фильтр.
        return list(acc_ids)
    try:
        rows = await _db_pool.fetch(
            """UPDATE tg_accounts
                  SET in_operation   = TRUE,
                      op_lease_owner = $2,
                      op_lease_until = now() + make_interval(secs => $3)
                WHERE id = ANY($1::int[])
                  AND (in_operation = FALSE
                       OR op_lease_until IS NULL
                       OR op_lease_until < now()
                       OR op_lease_owner = $2)
             RETURNING id""",
            [int(i) for i in acc_ids], _WORKER_ID, float(_LEASE_TTL_S),
        )
        return [int(r["id"]) for r in rows]
    except Exception as e:
        log.error(
            "op_worker: захват аренды не удался (%s) — НИЧЕГО не захватываем "
            "(fail-closed: сессия без арбитража рискует auth-key)", e,
        )
        return []


async def _db_release(acc_ids: list[int]) -> None:
    """Снять аренду — ТОЛЬКО со своих аккаунтов.

    Условие по op_lease_owner обязательно: без него рестарт/уборка одной реплики
    снимала бы in_operation с сессий, которые прямо сейчас держит другая.
    op_lease_owner IS NULL — legacy-строки до миграции аренды, их освобождаем.
    """
    if not _db_pool or not acc_ids:
        return
    try:
        await _db_pool.execute(
            """UPDATE tg_accounts
                  SET in_operation   = FALSE,
                      op_lease_until = NULL,
                      op_lease_owner = NULL
                WHERE id = ANY($1::int[])
                  AND (op_lease_owner = $2 OR op_lease_owner IS NULL)""",
            [int(i) for i in acc_ids], _WORKER_ID,
        )
    except Exception as e:
        log.warning("op_worker: снятие аренды не удалось: %s", e)


async def renew_leases() -> int:
    """Heartbeat: продлить аренду аккаунтов, которые процесс РЕАЛЬНО держит.

    Вызывается из цикла воркера. Пока процесс жив — его аккаунты не уводят;
    умер — аренды истекают, и флот возвращается в оборот сам.

    КРИТИЧНО: продлеваем ТОЛЬКО те, что сейчас в памяти (`_accounts_in_use`), а НЕ
    все `in_operation=TRUE` с нашим op_lease_owner. Иначе ЗАЛИПШИЙ флаг (операция
    упала/не сняла in_operation, но в памяти держателя уже нет) продлевался бы
    ВЕЧНО — аренда никогда не истекает, и аккаунт навсегда «занят операцией»:
    прогрев стоит (день 0/14), фабрика/операции получают «все аккаунты заняты».
    Флаг без живого держателя в памяти теперь НЕ продлеваем — аренда истекает за
    TTL, и _db_claim/warmup/reconcile забирают аккаунт обратно.
    Возвращает число продлённых строк.
    """
    if not _db_pool:
        return 0
    async with _accounts_lock:
        held = [int(a) for a in _accounts_in_use]
    if not held:
        return 0
    try:
        res = await _db_pool.execute(
            """UPDATE tg_accounts
                  SET op_lease_until = now() + make_interval(secs => $2)
                WHERE op_lease_owner = $1 AND in_operation = TRUE
                  AND id = ANY($3::int[])""",
            _WORKER_ID, float(_LEASE_TTL_S), held,
        )
        try:
            return int(str(res).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0
    except Exception as e:
        log.warning("op_worker: продление аренды не удалось: %s", e)
        return 0


# ── Circuit Breaker (аварийный выключатель) ──────────────────────────────────
# ── Предохранитель операций ───────────────────────────────────────────────────
# Автопауза владельца при повторных сбоях вынесена в services/op_circuit_breaker.py
# (распил монолита; там же общий межпроцессный расчёт через таблицу op_circuit_breaker).
# Импортируем имена обратно, чтобы контракт op_worker._circuit_breaker_* / _cb_* /
# circuit_breaker_status и т.п. не менялся. Пул задаётся в init_op_worker_pool ниже.
from services.op_circuit_breaker import (  # noqa: E402,F401
    _CIRCUIT_BREAKER_THRESHOLD,
    _CIRCUIT_BREAKER_COOLDOWN,
    _circuit_breaker_state,
    _circuit_breaker_lock,
    _cb_blank,
    _cb_apply,
    _cb_is_open,
    _cb_status,
    _cb_from_row,
    _cb_db_record,
    _cb_db_load,
    _circuit_breaker_record,
    _circuit_breaker_is_open,
    _circuit_breaker_status,
    circuit_breaker_is_open,
    circuit_breaker_status,
)


async def _release_op_for_circuit(pool: "asyncpg.Pool", op_id: int, cooldown_s: int) -> None:
    """Вернуть операцию в очередь при открытой цепи — с отложенным повтором.

    Поллер атомарно ставит операции status='running' ДО запуска задачи. Если
    цепь открыта, задача выходит, но операция осталась бы 'running' до stale-
    watchdog (60 мин) — держа фантомный слот параллельности владельца и блокируя
    его другие операции, хотя cooldown цепи всего 30 мин. Поэтому возвращаем в
    'pending' с scheduled_for = now()+cooldown+буфер: повтор ровно после cooldown,
    без busy-loop (кандидаты уважают scheduled_for) и без фантомного слота."""
    defer = max(int(cooldown_s), 0) + 5
    await _safe_execute(
        pool,
        # done_items=0: как retry/watchdog-requeue — при повторном прогоне после
        # cooldown исполнитель считает прогресс заново, иначе done копится поверх
        # прошлого прогона → счётчик done>total (класс #8).
        "UPDATE operation_queue SET status='pending', started_at=NULL, done_items=0, "
        "scheduled_for = now() + make_interval(secs => $2) WHERE id=$1"
        f" AND status NOT IN {op_status.sql_terminal_list()}",
        op_id,
        float(defer),
        log_ctx=f"[circuit_release op={op_id}]",
    )


_ACCT_WAIT_MAX_MIN = 20  # сколько ждать свободные аккаунты, потом — провал


async def _requeue_op_no_accounts(pool: "asyncpg.Pool", op_id: int, defer_s: int = 90) -> None:
    """Флот временно занят другими операциями — вернуть op в очередь с задержкой,
    а не проваливать. Ограничено по времени: если операция ждёт свободные
    аккаунты дольше _ACCT_WAIT_MAX_MIN — провалить (без вечного цикла).

    Это делает параллельные операции «живой очередью»: конкурентная операция не
    падает «все аккаунты заняты», а мягко ждёт и стартует, когда флот освободится
    (после честного дележа большинство операций стартуют сразу)."""
    import datetime as __dt
    row = await _safe_fetchrow(
        pool, "SELECT created_at FROM operation_queue WHERE id=$1", op_id,
        log_ctx=f"[requeue_no_acc op={op_id}]")
    if row and row.get("created_at"):
        age = __dt.datetime.now(__dt.timezone.utc) - row["created_at"]
        if age > __dt.timedelta(minutes=_ACCT_WAIT_MAX_MIN):
            await _safe_execute(
                pool,
                "UPDATE operation_queue SET status='failed', finished_at=now(), "
                "error_msg=$2 WHERE id=$1"
                f" AND status NOT IN {op_status.sql_terminal_list()}",
                op_id, "Флот занят другими операциями — не дождались свободных аккаунтов",
                log_ctx=f"[requeue_no_acc_fail op={op_id}]")
            return
    await _safe_execute(
        pool,
        "UPDATE operation_queue SET status='pending', started_at=NULL, done_items=0, "
        "scheduled_for = now() + make_interval(secs => $2) WHERE id=$1"
        f" AND status NOT IN {op_status.sql_terminal_list()}",
        op_id, float(defer_s),
        log_ctx=f"[requeue_no_acc op={op_id}]")


# Сколько секунд флуд-паузы ещё имеет смысл переждать ПРЯМО В ПРОГОНЕ.
# Telegram отдаёт FloodWait и на десятки секунд, и на часы. Короткую паузу
# дешевле переждать на месте; длинную — нельзя: прогон держит слот
# параллельности (один из восьми) и арендованные аккаунты всё это время, не делая
# ничего. Флот при этом простаивает, а с введением потолка прогона такой сон ещё
# и съедает его целиком, обрывая всю операцию.
_FLOOD_INLINE_MAX_S = _int_env("OP_FLOOD_INLINE_MAX_SEC", 15 * 60, 30, 6 * 3600)


async def bounded_flood_sleep(seconds: float, where: str = "") -> float:
    """Переждать флуд-паузу ВНУТРИ прогона, но не дольше _FLOOD_INLINE_MAX_S.

    Зачем предел. Пауза назначается ОДНОМУ аккаунту за ОДНО действие, а этот сон
    останавливает весь прогон: слот параллельности (один из восьми) и все
    арендованные аккаунты простаивают, хотя следующая цель обычно идёт через
    другой аккаунт и другой канал. Сон на несколько часов ради одного
    упёршегося аккаунта — это простой флота, а не осторожность.

    Ограничение НЕ обходит лимиты Telegram: штраф аккаунту уже записан в
    flood_engine, и дальше его держат пейсинг и карантин — то есть само
    флудившее действие раньше срока не повторяется. Ограничивается ровно то,
    сколько прогон стоит на месте.

    Там, где ждать надо всю паузу целиком, операция откладывается через
    _defer_op_for_flood: ждёт очередь, а не занятый слот.

    Возвращает фактическую длительность сна — чтобы вызывающий мог её залогировать.
    """
    want = max(0.0, float(seconds or 0))
    actual = min(want, float(_FLOOD_INLINE_MAX_S))
    if want > actual:
        log.warning(
            "op_worker%s: флуд-пауза %.0fс урезана до %.0fс — прогон не должен "
            "держать слот и флот столько времени",
            f" {where}" if where else "", want, actual,
        )
    if actual > 0:
        await asyncio.sleep(actual)
    return actual


async def _defer_op_for_flood(
    pool: "asyncpg.Pool", op_id: int, wait_s: int, reason: str = ""
) -> None:
    """Отложить операцию до конца длинной флуд-паузы, освободив слот и флот.

    Отдельно от _requeue_op_no_accounts: там отсчитывается _ACCT_WAIT_MAX_MIN от
    СОЗДАНИЯ операции и по его истечении операция проваливается. Для ожидания
    свободных аккаунтов это верно, а для флуда — нет: пауза назначена Telegram,
    ждать её штатно, и провалить операцию за то, что Telegram попросил подождать
    час, было бы наказанием за соблюдение правил платформы.

    done_items сбрасывается, как и на прочих путях возврата в очередь: прогресс
    пересчитается при возобновлении (исполнитель пропустит уже взятые цели по
    operation_log и снова их засчитает). Без сброса счётчик копился бы поверх
    прошлого прогона — класс «done > total».
    """
    delay = max(1, int(wait_s))
    await _safe_execute(
        pool,
        "UPDATE operation_queue SET status='pending', started_at=NULL, done_items=0, "
        "last_error=$3, scheduled_for = now() + make_interval(secs => $2) WHERE id=$1"
        f" AND status NOT IN {op_status.sql_terminal_list()}",
        op_id, float(delay), (reason or "")[:300],
        log_ctx=f"[defer_flood op={op_id}]")
    log.warning(
        "op_worker: op=%d отложена на %dс из-за длинной флуд-паузы — слот и аккаунты освобождены",
        op_id, delay,
    )


# ── Адаптивный темп по времени суток + ML ─────────────────────────────────────
# Расчёт адаптивной задержки вынесен в services/op_pacing.py (распил монолита).
# Импортируем обратно, чтобы op_worker.get_adaptive_delay / get_adaptive_batch_delay
# и таблицы множителей остались доступны как раньше (контракт не изменился).
from services.op_pacing import (  # noqa: E402,F401
    _HOUR_MULTIPLIER,
    _DAY_MULTIPLIER,
    get_adaptive_delay,
    get_adaptive_batch_delay,
)


async def reset_stale_in_operation(pool: "asyncpg.Pool") -> None:
    """Освободить ЗАЛИПШИЕ in_operation при старте — не трогая живые аренды.

    Раньше здесь был безусловный сброс всех флагов. С одной репликой это верно
    («прошлый процесс — это я»), но со второй репликой рестарт одного контейнера
    снимал защиту с сессий, которые прямо сейчас держит другой: обе реплики
    считали аккаунт свободным и открывали одну сессию дважды.

    Теперь освобождаем только то, за что никто живой не отвечает:
      • аренда истекла (процесс умер, продлевать некому);
      • аренды нет вовсе (legacy-строки до миграции v179);
      • аренда наша собственная (мы и есть перезапустившийся процесс).
    """
    try:
        res = await pool.execute(
            """UPDATE tg_accounts
                  SET in_operation   = FALSE,
                      op_lease_until = NULL,
                      op_lease_owner = NULL
                WHERE in_operation = TRUE
                  AND (op_lease_until IS NULL
                       OR op_lease_until < now()
                       OR op_lease_owner = $1)""",
            _WORKER_ID,
        )
        log.info("op_worker: освобождены залипшие аккаунты (%s); живые аренды сохранены", res)
    except Exception as e:
        log.warning("op_worker: failed to clear stale in_operation: %s", e)


def _fire_db_flag(acc_ids: list[int], value: bool) -> None:
    """Fire-and-forget DB update for in_operation flag (best-effort)."""
    if not _db_pool or not acc_ids:
        return
    try:
        # spawn держит strong-ссылку (класс 14): иначе GC мог бы собрать апдейт
        # флага in_operation до записи → рассинхрон изоляции/координации аккаунтов.
        # Нет running loop (sync-контекст) → spawn вернёт None, флаг просто пропущен.
        from services.bg_tasks import spawn
        spawn(_do_db_flag(acc_ids, value))
    except Exception as e:
        log.warning("op_worker: _fire_db_flag spawn failed: %s", e)


async def _do_db_flag(acc_ids: list[int], value: bool) -> None:
    if not _db_pool:
        return
    try:
        await _db_pool.execute(
            "UPDATE tg_accounts SET in_operation=$1 WHERE id = ANY($2::int[])",
            value, acc_ids,
        )
    except Exception as e:
        log.debug("op_worker: db flag update failed: %s", e)


# mark_accounts_in_use() УДАЛЁН намеренно (сторожит
# tests/test_session_concurrency_safety.py). Он помечал аккаунты занятыми
# БЕЗУСЛОВНО — заявлял «эти сессии мои», не спрашивая арбитра, — и поэтому
# пропускал в работу аккаунт, уже занятый другой операцией или репликой
# (вторая сессия на одном auth-key → AUTH_KEY_DUPLICATED, смерть сессии).
# Все его вызовы переведены на отказной try_claim_account(s). Не возвращать:
# нужна занятость — берите захват и честно обрабатывайте отказ.


async def release_accounts(acc_ids: list[int]) -> None:
    """Release accounts after operation completes.

    Updates both in-memory registry and database in_operation flag.

    Args:
        acc_ids: List of account IDs to release.
    """
    async with _accounts_lock:
        for aid in acc_ids:
            _accounts_in_use.discard(aid)
            for locked_acc_ids in _operation_account_locks.values():
                locked_acc_ids.discard(aid)
    await _db_release([int(a) for a in acc_ids])


async def try_claim_accounts(acc_ids: list[int]) -> list[int]:
    """Атомарно захватить СВОБОДНЫЕ аккаунты из списка под живые сессии.

    Возвращает подмножество реально захваченных id (те, что были свободны).
    Проверка занятости и захват — под одним `_accounts_lock`, поэтому между ними
    op_worker/другой цикл не может вклиниться (устранён TOCTOU проверки-затем-
    пометки). Единый арбитр `_accounts_in_use` для ВСЕХ живых сессий (операции +
    прогрев + призрак + пре-флайт) — одна auth-key сессия НИКОГДА не коннектится
    из двух мест (защита от AUTH_KEY_DUPLICATED).

    Освобождать через release_accounts(claimed) в finally у вызывающего.

    Два уровня: локальный фильтр (быстро отсекает своё) и аренда в БД
    (единственный арбитр, видимый другим репликам). Если БД дала меньше, чем
    отфильтровал локальный уровень — лишнее возвращаем обратно, иначе аккаунт
    остался бы «занят» в памяти навсегда, не будучи занятым на деле.
    """
    ids = [int(a) for a in acc_ids]
    async with _accounts_lock:
        local = [a for a in ids if a not in _accounts_in_use]
        _accounts_in_use.update(local)          # предварительный захват
    if not local:
        return []
    granted = await _db_claim(local)
    if len(granted) != len(local):
        lost = set(local) - set(granted)
        async with _accounts_lock:
            _accounts_in_use.difference_update(lost)   # откат непрошедших
    return granted


async def try_claim_account(acc_id: int) -> bool:
    """Атомарно захватить ОДИН аккаунт под живую фоновую сессию (ghost/warmup).

    True — аккаунт был свободен и теперь захвачен; False — уже занят операцией
    или другим циклом. Тонкая обёртка над try_claim_accounts (см. её докстринг:
    единый арбитр + защита от AUTH_KEY_DUPLICATED). Освобождать через
    release_accounts([acc_id]) в finally у вызывающего.
    """
    claimed = await try_claim_accounts([int(acc_id)])
    return bool(claimed)


async def release_operation_accounts(op_id: int) -> None:
    """Release every account claimed by an operation after executor errors.

    Args:
        op_id: Operation ID whose accounts to release.
    """
    freed: list[int] = []
    async with _accounts_lock:
        acc_ids = _operation_account_locks.pop(op_id, set())
        for aid in acc_ids:
            _accounts_in_use.discard(aid)
            freed.append(aid)
    await _db_release(freed)


async def _claim_available_accounts(
    op_id: int, accounts: list, owner_id: int | None = None
) -> list:
    """Atomically claim free accounts and bind them to operation cleanup.

    Честный дележ флота (fair-share): если у владельца ПАРАЛЛЕЛЬНО идут другие
    операции — один op не забирает весь свободный флот, а берёт справедливую долю
    (⌈флот / число параллельных операций⌉), чтобы конкурентные операции реально
    запускались, а не падали «все аккаунты заняты». В одиночку op берёт весь
    свободный флот (без потери пропускной способности). Захват дизъюнктен →
    одна сессия никогда не используется двумя операциями одновременно
    (защита от AUTH_KEY_DUPLICATED сохраняется)."""
    # Сколько операций владельца РЕАЛЬНО идут прямо сейчас — считаем ДО блокировки
    # (это await). Берём только операции, живые в памяти ЭТОГО процесса
    # (_active_op_ids), а не все строки status='running' в БД: залипшие 'running'
    # от упавших процессов (сброс сторожем только через 60 мин) иначе раздули бы
    # делитель, и ОДИНОЧНАЯ операция получила бы лишь долю флота вместо всего
    # свободного флота (симптом «стартует лишь несколько аккаунтов из N»).
    concurrency = 1
    if owner_id and _db_pool:
        try:
            async with _active_lock:
                active_ids = [int(i) for i in _active_op_ids]
            if active_ids:
                n = await _db_pool.fetchval(
                    "SELECT COUNT(*) FROM operation_queue "
                    "WHERE owner_id=$1 AND status='running' AND id = ANY($2::bigint[])",
                    owner_id, active_ids,
                )
                concurrency = max(1, int(n or 1))
            else:
                concurrency = 1
        except Exception:
            concurrency = 1
    async with _accounts_lock:
        free = [a for a in accounts if int(a["id"]) not in _accounts_in_use]
        # Доля флота на операцию при конкуренции. В одиночку (concurrency=1) —
        # весь свободный флот.
        if concurrency > 1 and accounts:
            share = max(_MIN_ACCOUNTS_PER_OP,
                        -(-len(accounts) // concurrency))  # ceil(len/concurrency)
            free = free[:share]
        claimed = free
        acc_ids = [int(a["id"]) for a in claimed]
        _accounts_in_use.update(acc_ids)
        if acc_ids:
            _operation_account_locks.setdefault(op_id, set()).update(acc_ids)
    if not acc_ids:
        return []
    # Межпроцессный арбитр: без него вторая реплика взяла бы те же сессии.
    granted = set(await _db_claim(acc_ids))
    if len(granted) != len(acc_ids):
        lost = set(acc_ids) - granted
        async with _accounts_lock:
            _accounts_in_use.difference_update(lost)
            held = _operation_account_locks.get(op_id)
            if held is not None:
                held.difference_update(lost)
                if not held:
                    _operation_account_locks.pop(op_id, None)
        claimed = [a for a in claimed if int(a["id"]) in granted]
    return claimed


async def _rebalance_claim(op_id: int, owner_id: int, held: list) -> list:
    """Честный дележ НА ЛЕТУ: отдать часть уже захваченных аккаунтов, если
    после старта операции появились другие ПАРАЛЛЕЛЬНЫЕ (running) операции
    того же владельца.

    _claim_available_accounts считает справедливую долю только в МОМЕНТ
    первого захвата. Долгая операция, стартовавшая в одиночку, забирает весь
    свободный флот и держит его до самого конца — вторая операция того же
    владельца ждёт полного завершения первой вместо совместного прогресса
    (жалоба владельца: «нужно, чтобы шли постепенно-параллельно, не падая
    из-за занятого флота»). Здесь та же формула доли, пересчитанная посреди
    прогона: если доля уменьшилась — лишнее возвращается в пул тем же путём,
    что и обычное освобождение (release_accounts), и почти сразу подхватывается
    той операцией, что ждёт (напрямую или через живую очередь _requeue_op_no_accounts).

    Не претендует на то, чтобы быть справедливым В КАЖДЫЙ момент — только
    сходится к честной доле со временем, не блокируя прогресс ни одной из
    операций и не открывая двух сессий на одном аккаунте одновременно."""
    if not held or not owner_id or not _db_pool:
        return held
    try:
        async with _active_lock:
            other_ids = [int(i) for i in _active_op_ids if int(i) != op_id]
        if not other_ids:
            return held
        n = await _db_pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue "
            "WHERE owner_id=$1 AND status='running' AND id = ANY($2::bigint[])",
            owner_id, other_ids,
        )
        concurrency = 1 + max(0, int(n or 0))
    except Exception:
        return held
    if concurrency <= 1:
        return held
    fair_share = max(_MIN_ACCOUNTS_PER_OP, -(-len(held) // concurrency))
    if len(held) <= fair_share:
        return held
    keep, give_back = held[:fair_share], held[fair_share:]
    await release_accounts([int(a["id"]) for a in give_back])
    log.info(
        "op=%d: отдал %d/%d аккаунтов в общий пул — конкурентных running-операций "
        "владельца стало %d (справедливая доля %d)",
        op_id, len(give_back), len(held), concurrency, fair_share,
    )
    return keep


def is_account_in_use(acc_id: int) -> bool:
    """Проверить занят ли аккаунт (non-async, читает snapshot).

    ВНИМАНИЕ: это снимок памяти ТОЛЬКО своего процесса и он не резервирует
    аккаунт. Для «занять под живую сессию» — try_claim_account(s): проверка и
    захват там неразделимы и видны другим репликам.
    """
    return acc_id in _accounts_in_use


async def _claim_single_account(
    pool: asyncpg.Pool, owner_id: int, params: dict
) -> "tuple[dict | None, dict | None]":
    """Достать аккаунт одиночной операции и АТОМАРНО захватить его под сессию.

    Возвращает (acc, None) при успехе либо (None, готовый результат-отказ) —
    вызывающий сразу возвращает второй элемент. Освобождать обязан сам:
    `await release_accounts([int(acc["id"])])` в finally.

    Один пролог на все одиночные исполнители (выход из чатов, чтение диалогов,
    удаление переписки/контактов): раньше он был скопирован в каждый, и во всех
    копиях НЕ ХВАТАЛО захвата — операция открывала живую сессию на аккаунте,
    который в этот момент мог вести массовую операцию или прогрев. Две сессии на
    одном auth-key → AUTH_KEY_DUPLICATED.
    """
    account_id = params.get("account_id")
    if not account_id:
        return None, {"status": "failed", "summary": "⚠️ account_id не указан"}
    try:
        acc = await pool.fetchrow(
            "SELECT *, (SELECT proxy_url FROM user_proxies up "
            "WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 "
            "AND is_active=TRUE AND session_str IS NOT NULL",
            int(account_id), owner_id,
        )
    except Exception as exc:
        return None, {"status": "failed", "summary": f"⚠️ Ошибка получения аккаунта: {exc}"}
    if not acc:
        return None, {"status": "failed",
                      "summary": "⚠️ Аккаунт не найден, отключён или нет сессии"}
    if not await try_claim_account(int(acc["id"])):
        return None, {"status": "requeue",
                      "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    return dict(acc), None


# ── Глобальный губернатор темпа ─────────────────────────────────────────────
async def _governor_mult(pool, owner_id: int) -> float:
    """Живой множитель темпа от губернатора (≥1.0). Fail-open → 1.0."""
    try:
        from services import fleet_governor
        return await fleet_governor.tempo_multiplier(pool, owner_id)
    except Exception:
        return 1.0


async def _governed_delay(pool, owner_id: int, base: float) -> float:
    """Базовая пауза, растянутая по давлению флота (НЕ трогает flood-паузы)."""
    return float(base) * await _governor_mult(pool, owner_id)


async def _governed_sleep(pool, owner_id: int, base: float) -> None:
    await asyncio.sleep(await _governed_delay(pool, owner_id, base))


async def _on_account_banned(pool, owner_id: int, acc_id: int, where: str,
                             *, bot=None) -> None:
    """Реакция на бан: мгновенно сбросить кэш губернатора (чтобы темп упал СЕЙЧАС,
    а не через 45с), записать событие в память организма и проверить, не идёт ли
    ВОЛНА банов (Anti-Storm). Fail-open."""
    try:
        from services import fleet_governor
        fleet_governor.invalidate(owner_id)
    except Exception:
        pass
    try:
        from services.organism import spine
        await spine.emit(pool, owner_id, "ban", {"account_id": acc_id, "where": where})
    except Exception:
        pass
    # Anti-Storm: событийный предохранитель. Считает РАЗНЫЕ пострадавшие аккаунты
    # за окно и при всплеске переводит флот в глубокий сон, переждать чистку.
    try:
        from services import anti_storm
        await anti_storm.check_and_arm(pool, owner_id, bot=bot)
    except Exception:
        pass


# Решение владельца: приглашённым НЕ слать приветствие в личку. Свежеприглашённый
# незнакомец, которому в ЛС падает автосообщение, — это и триггер антиспама
# Telegram, и повод уйти из чата (людей и так заваливает уведомлениями при
# инвайте). Выключатель хранит машинерию (A/B, дедуп, retention-маркер) на случай
# осознанного включения в будущем, но по умолчанию ЛС-приветствие не уходит.
_INVITE_WELCOME_DM_ENABLED = False


async def _chain_welcome(pool, owner_id: int, op_id: int, params: dict) -> None:
    """Шов «Инвайт → Welcome»: после успешного инвайта разослать приветствие тем,
    кто РЕАЛЬНО добавлен (operation_log.status='ok'). Только методы, где ok = член
    группы (direct/admin) — при 'link' само приглашение и есть DM. Fail-open.

    По решению владельца ЛС-приветствие приглашённым отключено (см.
    _INVITE_WELCOME_DM_ENABLED): даже с заполненным полем welcome ничего не
    уходит, чтобы не спамить и не выгонять только что вступивших."""
    if not _INVITE_WELCOME_DM_ENABLED:
        return
    w = params.get("welcome")
    if not isinstance(w, dict):
        return
    method = str(params.get("invite_method") or "direct").lower()
    if method not in ("direct", "admin"):
        return
    from services import ab_engine
    # A/B welcome: несколько вариантов приветствия → делим вступивших поровну и
    # меряем доставку так же, как рассылку (ab_batch/ab_variant, экран A/B).
    variants = ab_engine.clean_variants(w.get("variants"))
    text = (w.get("text") or "").strip()
    if not variants and not text:
        return
    try:
        # Отбираем по МАРКЕРУ ИСХОДА, а не по голому 'ok'. Раньше фильтр
        # исключал мета-цель 'promote', но НЕ 'promote_trick' — и приветствие
        # уходило в личку несуществующему пользователю с таким именем: пустая
        # трата отправки, мусор в журнале и лишний повод для флуда.
        rows = await _safe_fetch(
            pool,
            "SELECT DISTINCT target FROM operation_log WHERE op_id=$1 AND status='ok' "
            "AND message='joined' AND target IS NOT NULL", op_id)
        _meta = {"promote", "promote_trick"}
        targets = [r["target"] for r in (rows or []) if r["target"] and r["target"] not in _meta]
        if not targets:
            return
        acc_ids = [int(x) for x in (params.get("account_ids") or []) if str(x).isdigit()]
        if not acc_ids:
            arows = await _safe_fetch(
                pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND session_str IS NOT NULL", owner_id)
            acc_ids = [int(r["id"]) for r in (arows or [])]
        if not acc_ids:
            return
        delay = int(w.get("delay") or 45)
        from services import operation_bus
        import time as _t
        _CH = 1000
        # мс-гранулярность: два welcome-A/B в одну секунду не сольются в один батч
        ab_batch = int(_t.time() * 1000) if variants else None
        if variants:
            groups = ab_engine.split_audience(targets, len(variants))
            plan = [(variants[i], f"A/B#{i + 1}", groups[i]) for i in range(len(variants))]
        else:
            plan = [(text, "", targets)]
        for vtext, vlabel, vtargets in plan:
            for i in range(0, len(vtargets), _CH):
                chunk = vtargets[i:i + _CH]
                if not chunk:
                    continue
                tag = (vlabel + " ") if vlabel else ""
                await operation_bus.submit(
                    pool, owner_id, "bulk_dm_adhoc",
                    {"account_ids": acc_ids, "usernames": chunk, "text": vtext,
                     "delay": delay, "ab_variant": vlabel or None, "ab_batch": ab_batch},
                    total_items=len(chunk),
                    label=f"{tag}Welcome вступившим ч.{i // _CH + 1}: {len(chunk)}")
        log.info("chain_welcome op=%d owner=%s → %d получателей (A/B=%s)",
                 op_id, owner_id, len(targets), len(variants) or 0)
    except Exception:
        log_exc_swallow(log, f"_chain_welcome op={op_id}")


# ── Retry Intelligence ─────────────────────────────────────────────────────────
# Классификация ошибок операций и нормализация результата вынесены в
# services/op_errors.py (распил монолита). Импортируем обратно, чтобы
# op_worker._classify_op_error / _FATAL_ERRORS / _normalize_result и т.п.
# остались доступны как раньше (публичный/приватный контракт не изменился).
from services import op_status
from services.op_errors import (  # noqa: E402,F401
    _RETRYABLE_ERRORS,
    _FATAL_ERRORS,
    _FATAL_MSG_PATTERNS,
    _FLOOD_PATTERNS,
    _PEER_FLOOD_PATTERNS,
    _NETWORK_PATTERNS,
    _SESSION_CONFLICT_PATTERNS,
    _DEAD_SESSION_PATTERNS,
    _is_session_conflict_error,
    _normalize_result,
    _classify_op_error,
    _is_network_or_proxy_error,
    _is_dead_session_error,
)


async def _record_network_isolation(
    pool: asyncpg.Pool,
    account_id: int,
    action_type: str,
    operation_id: int,
    error_text: str,
    cooldown_s: int = 15 * 60,
) -> None:
    await _safe_execute(
        pool,
        """UPDATE tg_accounts
           SET cooldown_until = GREATEST(
                   COALESCE(cooldown_until, NOW()),
                   NOW() + ($2 * INTERVAL '1 second')
               ),
               acc_status = 'cooldown',
               status_reason = $3
           WHERE id = $1""",
        account_id,
        cooldown_s,
        f"network/proxy failure ({action_type}): {error_text[:180]}",
        log_ctx=f"[network_isolation acc={account_id}]",
    )
    try:
        from services import account_health

        account_health.update_after_failure(account_id, action_type, is_flood=False)
    except Exception:
        log_exc_swallow(
            log,
            f"op_worker: account_health network isolation failed for account_id={account_id}",
        )
    await _safe_execute(
        pool,
        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,0,$2,'error',$3)",
        operation_id,
        str(account_id),
        f"Аккаунт изолирован на {cooldown_s}s из-за сетевого/прокси сбоя: {error_text[:160]}",
        log_ctx=f"[network_isolation_log op={operation_id}]",
    )


async def _deactivate_dead_session(
    pool: asyncpg.Pool, exc: Exception, params: dict
) -> None:
    """При AUTH_KEY/SESSION_REVOKED ошибке немедленно деактивировать аккаунт в БД.

    Без этого аккаунт продолжает попадать в выборки resource_selector до следующего
    цикла account_monitor (1 час), и все операции на нём будут падать.
    Используем acc_status='session_expired' + is_active=FALSE — обе колонки
    уже существуют в tg_accounts (schema_v15 + schema_v40).
    """
    msg = str(exc)
    name = type(exc).__name__
    if not (name in _FATAL_ERRORS or "SESSION_REVOKED" in msg or "AUTH_KEY" in msg):
        return
    account_ids = params.get("account_ids") or []
    if not account_ids:
        return
    for acc_id in account_ids:
        try:
            result = await pool.execute(
                """UPDATE tg_accounts
                   SET is_active    = FALSE,
                       acc_status   = 'session_expired',
                       status_reason = $2
                   WHERE id = $1 AND is_active = TRUE""",
                int(acc_id),
                f"AUTH_KEY/SESSION dead (op_worker): {msg[:200]}",
            )
            if result != "UPDATE 0":
                log.warning(
                    "op_worker: deactivated dead session account_id=%s (%s)",
                    acc_id,
                    name,
                )
        except Exception as db_err:
            log.warning(
                "op_worker: failed to deactivate account_id=%s: %s", acc_id, db_err
            )


async def _maybe_requeue(
    pool: asyncpg.Pool, op_id: int, exc: Exception, params: dict, op_type: str
) -> bool:
    """
    Если ошибка ретраевая и retry_count < max_retries — сбросить операцию в pending.
    Возвращает True если операция поставлена на повторную попытку.
    
    Smart Retry features:
    - FloodWait: wait exactly as Telegram requests + jitter
    - PeerFlood: rotate to different account, 48h cooldown
    - AUTH_KEY dead: immediate deactivation, no retry
    - CHANNEL_PRIVATE: skip channel permanently
    """
    kind = _classify_op_error(exc)
    if kind in ("fatal", "skip"):
        return False

    row = await _safe_fetchrow(
        pool,
        "SELECT retry_count, max_retries FROM operation_queue WHERE id=$1",
        op_id,
        log_ctx=f"[maybe_requeue op={op_id}]",
    )
    if not row:
        return False
    retry_count = (row["retry_count"] or 0) + 1
    max_retries = row["max_retries"] or 3

    if retry_count > max_retries:
        return False

    flood_wait = extract_flood_wait(exc, str(exc))
    
    # Smart backoff calculation
    if kind == "peer_flood":
        # PeerFlood: 48h cooldown + rotate account
        backoff = 48 * 3600
        log.info("op_worker: PeerFlood for op=%d — 48h cooldown, rotate account", op_id)
    elif kind == "flood" and flood_wait > 0:
        # FloodWait: wait exactly as Telegram requests + 60s jitter
        backoff = min(flood_wait + 60, 24 * 3600)
        log.info("op_worker: FloodWait %ds for op=%d — waiting %ds", flood_wait, op_id, backoff)
    else:
        # Exponential backoff with jitter for other errors
        backoff = min(30 * (2 ** (retry_count - 1)), 600)
        backoff = int(backoff * (0.8 + 0.4 * random.random()))  # ±20% jitter

    account_ids = [int(acc_id) for acc_id in (params.get("account_ids") or [])]
    try:
        from services import flood_engine

        for account_id in account_ids:
            if kind == "peer_flood":
                await flood_engine.record_peer_flood(
                    pool,
                    account_id,
                    action_type=op_type,
                    operation_id=op_id,
                )
            elif kind == "flood":
                await flood_engine.record_flood(
                    pool,
                    account_id,
                    flood_wait or 60,
                    action_type=op_type,
                    operation_id=op_id,
                )
    except Exception as penalty_exc:
        log.warning(
            "op_worker: failed to persist %s penalty for op %d: %s",
            kind,
            op_id,
            penalty_exc,
        )

    res = await _safe_execute(
        pool,
        """UPDATE operation_queue
            SET status='pending',
                retry_count=$1,
                last_error=$2,
                scheduled_for=now() + make_interval(secs => $4::numeric),
                started_at=NULL,
                done_items=0
            WHERE id=$3
              AND status NOT IN """ + op_status.sql_terminal_list(),
        retry_count,
        str(exc)[:300],
        op_id,
        float(backoff),
        log_ctx=f"[maybe_requeue_update op={op_id}]",
    )
    # Ноль строк = операция уже терминальна. Практически всегда это ОТМЕНА
    # владельцем во время прогона: он нажал «Отменить», исполнитель оборвался
    # ошибкой, и повтор вернул бы операцию в очередь — отмена молча отменялась
    # бы, а операция запускалась заново. Честно говорим «не поставлена».
    if str(res or "").strip().endswith(" 0"):
        log.info(
            "op_worker: op %d уже в терминальном статусе — повтор НЕ ставим "
            "(отмена владельца сильнее повтора)", op_id,
        )
        return False
    log.info(
        "op_worker: op %d queued for retry %d/%d in %ds",
        op_id,
        retry_count,
        max_retries,
        backoff,
    )
    return True


# ── Аккаунты для массовых операций: ОДИН источник правды ─────────────────────
# Этот SELECT был скопирован по файлу девять раз, и во всех копиях не хватало трёх
# полей, которые читает `account_manager._make_client` при выборе транспорта:
#
#   • `proxy_id` — «привязан ли аккаунт к прокси». Ключевой момент: если прокси
#     назначен, но сейчас НЕАКТИВЕН,JOIN отдаёт proxy_url=NULL. Без proxy_id
#     аккаунт выглядит «свободным», и политика разрешает подключить его НАПРЯМУЮ
#     — а он привязан к IP того прокси. Telegram отвечает AUTH_KEY_DUPLICATED и
#     УБИВАЕТ сессию. Аккаунт после этого не работает вообще; снаружи это ровно
#     «операция ничего не делает». Об этой ловушке прямо предупреждает комментарий
#     в `account_manager._pick_proxy`, но массовые исполнители поле не выбирали.
#   • `cf_relay_url` — транспорт через Cloudflare-релей. Без него аккаунт на релее
#     терял свой транспорт и уходил напрямую с адреса сервера.
#   • `owner_id` — по нему `_make_client` находит ПОЛИТИКУ прокси владельца
#     (`_OWNER_PROXY_POLICY`). Без него строгая политика, включённая пользователем,
#     в массовых операциях молча не применялась.
#
# Дальше — один текст на всех: разойтись копиям больше негде.
# _ACC_COLS удалён: все использования переведены на resource_selector.select_all_active


def _acc_has_bound_proxy(acc) -> bool:
    """Привязан ли аккаунт к КОНКРЕТНОМУ прокси: назначен proxy_id ЛИБО есть активный
    proxy_url. Зеркалит `has_assigned_proxy` в account_manager._resolve_client_proxy.

    Аккаунт БЕЗ такой привязки ходит напрямую/через пул — его сбой подключения это НЕ
    «прокси недоступен», и смена IP ему auth key не ломает (нет IP, к которому сессия
    привязана). Поэтому советовать такому аккаунту «исправьте прокси» — ложь, которая
    уводит пользователя чинить несуществующую проблему."""
    try:
        return bool(acc.get("proxy_id")) or bool(str(acc.get("proxy_url") or "").strip())
    except AttributeError:
        return False


def _proxy_skip_reason(acc) -> tuple[str, str]:
    """(текст в лог, текст пользователю) при изоляции аккаунта после proxy_error в
    bound-режиме. Для привязанного к прокси — «исправьте прокси»; для аккаунта, которому
    прокси НЕ назначали, — честно про сессию/сеть (см. `_acc_has_bound_proxy`)."""
    if _acc_has_bound_proxy(acc):
        return (
            "прокси недоступен — пропуск оставшихся (смена IP ломает auth key)",
            "❌ Прокси недоступен — смена IP ломает auth key. Исправьте прокси.",
        )
    return (
        "без назначенного прокси не подключился (сессия/сеть) — пропуск оставшихся",
        "❌ Не удалось подключиться (сессия/сеть). Аккаунту прокси не назначен — "
        "проверьте сеть/сессию или назначьте прокси (это не проблема прокси).",
    )


def interleave_by_source(pairs: list) -> list:
    """Разложить цели так, чтобы соседние шли из РАЗНЫХ источников.

    Аудитория приходит из БД сгруппированной: сначала все, кого спарсили из
    одного канала, потом все из следующего. При инвайте это заметная подпись:
    аккаунт подряд приглашает сорок человек из @channelX, потом другой аккаунт —
    сорок из @channelY. Люди из одного источника похожи между собой (близкие
    id, общая подписка, часто общий интерес), и такой залп читается ровно как
    то, чем является: выгрузили список и залили пачкой.

    Перемешиваем не случайно, а РАВНОМЕРНО: каждому элементу даётся дробная
    позиция внутри своего источника, и всё сортируется по ней. Так источник из
    двух человек не слипнется в начале, а источник из двухсот не осядет
    сплошным хвостом — что случилось бы при простом круговом обходе, когда
    маленькие вёдра кончаются раньше.

    Один источник (или ни одного) — возвращаем как есть: перетасовывать нечего,
    а лишняя перестановка только сломала бы понятный пользователю порядок.
    """
    buckets: dict[str, list] = {}
    for ref, src in pairs:
        buckets.setdefault(str(src or ""), []).append(ref)
    if len(buckets) <= 1:
        return [ref for ref, _ in pairs]

    ranked: list[tuple[float, str, object]] = []
    for key, items in buckets.items():
        n = len(items)
        for i, ref in enumerate(items):
            ranked.append(((i + 0.5) / n, key, ref))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [ref for _, _, ref in ranked]


async def bump_daily_stats(
    pool: asyncpg.Pool,
    account_id: int,
    *,
    ok: int = 0,
    fail: int = 0,
    floods: int = 0,
    invites: int = 0,
) -> None:
    """Накопить суточные счётчики аккаунта в `account_daily_stats`.

    Таблица существовала с schema_v41, но НИКОГДА не заполнялась — это прямо
    признано в комментарии `infra_analytics.cb_infra_daily_stats`, где запрос
    пришлось переписать на operation_audit в обход неё. Из-за этого у системы не
    было суточной истории «сколько приглашено / сколько успешно / сколько флудов»
    на аккаунт, а без неё невозможны ни самообучающийся лимитер, ни карточка
    аккаунта, ни аналитика по флоту.

    UPSERT по (account_id, stat_date): счётчики накапливаются за сутки.
    Никогда не бросает — статистика не должна ронять массовую операцию.
    """
    if not account_id or not (ok or fail or floods or invites):
        return
    try:
        await pool.execute(
            """INSERT INTO account_daily_stats(
                   account_id, stat_date, actions_ok, actions_fail,
                   flood_events, invites_ok)
               VALUES($1, CURRENT_DATE, $2, $3, $4, $5)
               ON CONFLICT (account_id, stat_date) DO UPDATE SET
                   actions_ok   = account_daily_stats.actions_ok   + EXCLUDED.actions_ok,
                   actions_fail = account_daily_stats.actions_fail + EXCLUDED.actions_fail,
                   flood_events = account_daily_stats.flood_events + EXCLUDED.flood_events,
                   invites_ok   = account_daily_stats.invites_ok   + EXCLUDED.invites_ok""",
            int(account_id), int(ok), int(fail), int(floods), int(invites),
        )
    except Exception:
        log.debug("bump_daily_stats failed acc=%s", account_id, exc_info=True)


async def _audit(
    pool: asyncpg.Pool,
    owner_id: int,
    action: str,
    result: str,
    operation_id: int | None = None,
    account_id: int | None = None,
    target: str | None = None,
    error_msg: str | None = None,
    flood_wait_s: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Записать событие в operation_audit. Никогда не бросает исключений."""
    try:
        await pool.execute(
            """INSERT INTO operation_audit(
                   owner_id, operation_id, account_id, action, target,
                   result, error_msg, flood_wait_s, duration_ms
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            owner_id,
            operation_id,
            account_id,
            action,
            target,
            result,
            error_msg,
            flood_wait_s,
            duration_ms,
        )
    except Exception as e:
        log.warning(
            "audit write failed for op=%s action=%s: %s", operation_id, action, e
        )


async def write_op_audit(
    pool: asyncpg.Pool,
    owner_id: int,
    action: str,
    result: str,
    target: str | None = None,
    account_id: int | None = None,
    error_msg: str | None = None,
    flood_wait_s: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Public wrapper for _audit — use from handlers that bypass op_worker queue."""
    await _audit(
        pool,
        owner_id=owner_id,
        action=action,
        result=result,
        operation_id=None,
        account_id=account_id,
        target=target,
        error_msg=error_msg,
        flood_wait_s=flood_wait_s,
        duration_ms=duration_ms,
    )


_active_op_ids: set[int] = set()
# Strong-ссылки на запущенные задачи операций. _active_op_ids хранит лишь int-id;
# сам объект задачи, созданный create_task, event loop держит только СЛАБОЙ ссылкой
# → без этого набора GC мог бы собрать выполняющуюся операцию до завершения (тихая
# смерть на самом критичном пути). done-callback снимает ссылку по завершении.
_active_op_tasks: set[asyncio.Task] = set()
_active_lock = asyncio.Lock()

# Процесс получил SIGTERM и сворачивается (см. shutdown()). Поллер очереди
# перестаёт забирать новые операции: взять операцию в 'running' за секунду до
# закрытия пула — значит оставить её сиротой в очереди и потратить единицу
# бюджета живучести ни за что.
_shutting_down = False

# Per-owner semaphores: не более _MAX_PARALLEL_PER_OWNER параллельных операций на владельца
_owner_semaphores: dict[int, asyncio.Semaphore] = {}
_owner_sem_lock = asyncio.Lock()


async def _get_owner_semaphore(owner_id: int) -> asyncio.Semaphore:
    """Вернуть (или создать) семафор для конкретного owner_id."""
    async with _owner_sem_lock:
        if owner_id not in _owner_semaphores:
            _owner_semaphores[owner_id] = asyncio.Semaphore(_MAX_PARALLEL_PER_OWNER)
        return _owner_semaphores[owner_id]


# Track last progress milestone notified per op (25/50/75%)
_progress_milestones: dict[int, int] = {}

# Обслуживающие операции: пользователю НЕ нужен поминутный прогресс, только итог.
# Иначе длинная синхронизация контактов флота (28 акк.) спамила «0%» каждую минуту
# (жалоба: «уведомления об одной и той же операции каждую минуту»). Для них монитор
# прогресса не запускаем — придёт одно финальное уведомление.
_QUIET_PROGRESS_OPS = {"contacts_sync"}

# Cache for _is_cancelled: op_id -> (result: bool, checked_at: float)
_cancel_cache: dict[int, tuple[bool, float]] = {}
_CANCEL_CACHE_TTL = 5.0  # seconds between DB checks in tight loops

# ETA tracking: op_id -> {start_time, start_done, last_done, last_time}
_eta_data: dict[int, dict] = {}


async def _progress_monitor(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, op_type: str
) -> None:
    """Enhanced progress monitor with ETA calculation and milestone notifications."""
    _progress_milestones[op_id] = 0
    _heartbeat_sent = False
    _ticks_without_total = 0
    _last_update_sent = 0.0
    _last_sent_pct = -1  # чтобы не слать одинаковый прогресс повторно (застрявшая op)
    _eta_data[op_id] = {"start": time.time(), "last_done": 0, "last_time": time.time()}
    
    try:
        while True:
            await asyncio.sleep(15)
            try:
                row = await _safe_fetchrow(
                    pool,
                    "SELECT total_items, done_items, status FROM operation_queue WHERE id=$1",
                    op_id,
                    log_ctx=f"[progress_monitor op={op_id}]",
                )
                if not row or row["status"] != "running":
                    break
                total = row["total_items"] or 0
                done = row["done_items"] or 0
                
                if total <= 0:
                    # total_items not set yet — send a "still running" heartbeat
                    _ticks_without_total += 1
                    if _ticks_without_total >= 8 and not _heartbeat_sent:
                        _heartbeat_sent = True
                        await db.notify_if_enabled(
                            pool, bot, owner_id, "op_complete",
                            f"⏳ <b>Операция #{op_id}</b> — в процессе…\n"
                            f"<code>{op_type}</code>",
                        )
                    continue
                _ticks_without_total = 0
                pct = int(done * 100 / total)
                
                # Calculate ETA
                eta = _calculate_eta(op_id, done, total)
                eta_text = f"⏰ ETA: {eta}" if eta else ""
                
                # Send update every 30 seconds (not just milestones), но ТОЛЬКО если
                # прогресс реально сдвинулся — иначе застрявшая на 0% операция
                # слала бы одинаковое уведомление каждые 30с.
                now = time.time()
                if now - _last_update_sent >= 30 and pct != _last_sent_pct:
                    _last_update_sent = now
                    _last_sent_pct = pct
                    bar_filled = pct // 10
                    bar = "█" * bar_filled + "░" * (10 - bar_filled)
                    speed = _calculate_speed(op_id, done)
                    speed_text = f"⚡ {speed}/мин" if speed else ""
                    
                    await db.notify_if_enabled(
                        pool, bot, owner_id, "op_complete",
                        f"⏳ <b>Операция #{op_id}</b> — {pct}%\n"
                        f"[{bar}] {done}/{total}\n"
                        f"<code>{op_type}</code> {speed_text} {eta_text}",
                    )
                
                # Milestone notifications (25/50/75%)
                last = _progress_milestones.get(op_id, 0)
                milestone = None
                for m in (25, 50, 75):
                    if pct >= m > last:
                        milestone = m
                        break
                if milestone is not None:
                    _progress_milestones[op_id] = milestone
                    bar_filled = milestone // 10
                    bar = "█" * bar_filled + "░" * (10 - bar_filled)
                    from aiogram.utils.keyboard import InlineKeyboardBuilder
                    from bot.callbacks import BmCb

                    kb = InlineKeyboardBuilder()
                    kb.button(
                        text="📋 Очередь операций", callback_data=BmCb(action="op_reports")
                    )
                    await db.notify_if_enabled(
                        pool, bot, owner_id, "op_complete",
                        f"⏳ <b>Операция #{op_id}</b> — {milestone}%\n"
                        f"[{bar}] {done}/{total}\n"
                        f"<code>{op_type}</code> {eta_text}",
                        reply_markup=kb.as_markup(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("_progress_monitor: error for op %d: %s", op_id, e)
    except asyncio.CancelledError:
        pass
    finally:
        _progress_milestones.pop(op_id, None)
        _cancel_cache.pop(op_id, None)
        _eta_data.pop(op_id, None)


def _calculate_eta(op_id: int, done: int, total: int) -> str | None:
    """Calculate ETA based on processing speed."""
    data = _eta_data.get(op_id)
    if not data or done <= 0:
        return None
    
    elapsed = time.time() - data["start"]
    if elapsed < 30:  # Need at least 30s of data
        return None
    
    speed = done / elapsed  # items per second
    remaining = total - done
    if speed <= 0:
        return None
    
    eta_seconds = remaining / speed
    if eta_seconds < 60:
        return f"~{int(eta_seconds)}с"
    elif eta_seconds < 3600:
        return f"~{int(eta_seconds / 60)}мин"
    else:
        return f"~{int(eta_seconds / 3600)}ч {int((eta_seconds % 3600) / 60)}мин"


def _calculate_speed(op_id: int, done: int) -> str | None:
    """Calculate current processing speed."""
    data = _eta_data.get(op_id)
    if not data or done <= 0:
        return None
    
    elapsed = time.time() - data["start"]
    if elapsed < 10:
        return None
    
    speed = done / elapsed * 60  # items per minute
    if speed < 1:
        return f"{speed:.1f}"
    else:
        return f"{int(speed)}"


async def _reset_stale_running(pool: asyncpg.Pool) -> None:
    """При старте воркера сбрасывает операции в статусе 'running' обратно в 'pending'.

    Нужно после SIGTERM/рестарта: операции могли оказаться в running-состоянии
    без реально работающей задачи. Сбрасываем их с очисткой started_at, чтобы
    они были подхвачены воркером заново.

    Воскрешение ОГРАНИЧЕНО _MAX_REVIVES: операция, которая роняет сам воркер,
    иначе поднималась бы после каждого рестарта вечно (см. _MAX_REVIVES).
    """
    await _ensure_revive_column(pool)
    # Сначала — исчерпавшие бюджет: честный терминальный статус вместо ещё
    # одного круга. Порядок важен: иначе тот же UPDATE ниже снова поднял бы их.
    poisoned = await _safe_execute(
        pool,
        """UPDATE operation_queue
           SET status = 'failed', finished_at = now(), started_at = NULL,
               error_msg = COALESCE(error_msg, $2)
           WHERE status = 'running' AND COALESCE(revive_count, 0) >= $1""",
        _MAX_REVIVES,
        _POISON_ERROR.format(n=_MAX_REVIVES),
        log_ctx="[reset_stale_poison]",
    )
    try:
        poisoned_n = int(str(poisoned).split()[-1])
    except (ValueError, IndexError):
        poisoned_n = 0
    if poisoned_n:
        log.error(
            "op_worker startup: %d operations exceeded revive budget (%d) → failed",
            poisoned_n, _MAX_REVIVES,
        )
    result = await _safe_execute(
            pool,
        # done_items=0: операцию подхватит воркер заново и исполнитель прогонит её
        # с нуля — без сброса счётчик копился бы поверх прошлого прогона (класс
        # «done>total»/«34/17», как и в _maybe_requeue).
        """UPDATE operation_queue
           SET status = 'pending', started_at = NULL, done_items = 0,
               revive_count = COALESCE(revive_count, 0) + 1
           WHERE status = 'running'""",
        log_ctx="[reset_stale_revive]",
    )
    # asyncpg возвращает строку вида "UPDATE N"
    try:
        count = int(str(result).split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count:
        log.warning(
            "op_worker startup: reset %d stale 'running' operations → 'pending'",
            count,
        )
    else:
        log.info("op_worker startup: no stale running operations found")


async def _watchdog_stale(pool: asyncpg.Pool) -> None:
    """Периодически сбрасывает 'running' операции, которые висят дольше N минут.

    Исключает операции, которые реально выполняются в памяти (_active_op_ids),
    чтобы не перезапустить strike/bulk-op пока первый прогон ещё идёт.
    """
    try:
        async with _active_lock:
            active_now: frozenset[int] = frozenset(_active_op_ids)

        active_ids_list = list(active_now) if active_now else None

        # Исчерпавшие бюджет живучести — в failed, а не на очередной круг.
        # Без этого операция, которая ВЕШАЕТ исполнителя (а не роняет его),
        # раз в 60 минут бесконечно забирала слот и аккаунты флота: сторож
        # исправно возвращал её в pending, воркер исправно подхватывал, и так
        # до ручного вмешательства владельца.
        poisoned = await pool.execute(
            """UPDATE operation_queue
                SET status = 'failed', finished_at = now(), started_at = NULL,
                    error_msg = COALESCE(error_msg, $4)
                WHERE status = 'running'
                  AND started_at < now() - make_interval(mins => $1)
                  AND ($2::bigint[] IS NULL OR id != ALL($2::bigint[]))
                  AND COALESCE(revive_count, 0) >= $3""",
            _STALE_RUNNING_TIMEOUT_MIN,
            active_ids_list,
            _MAX_REVIVES,
            _POISON_ERROR.format(n=_MAX_REVIVES),
        )
        poisoned_n = int((poisoned or "UPDATE 0").split()[-1])
        if poisoned_n:
            log.error(
                "op_worker watchdog: %d hung ops exceeded revive budget (%d) → failed",
                poisoned_n, _MAX_REVIVES,
            )

        result = await pool.execute(
            # done_items=0 при пере-подхвате зависшей op — исполнитель прогоняет с
            # нуля, счётчик не должен копиться поверх прошлого (класс «done>total»).
            """UPDATE operation_queue
                SET status = 'pending', started_at = NULL, done_items = 0,
                    revive_count = COALESCE(revive_count, 0) + 1
                WHERE status = 'running'
                  AND started_at < now() - make_interval(mins => $1)
                  AND ($2::bigint[] IS NULL OR id != ALL($2::bigint[]))""",
            _STALE_RUNNING_TIMEOUT_MIN,
            active_ids_list,
        )
        count = int((result or "UPDATE 0").split()[-1])
        if count:
            log.warning("op_worker watchdog: reset %d stale running ops → pending", count)
    except Exception as e:
        log_exc_swallow(log, f"op_worker watchdog error: {e}")


async def _reconcile_in_operation(pool: asyncpg.Pool) -> None:
    """Снять залипший in_operation=TRUE с аккаунтов, которые НЕ держит ни одна
    живая операция (in-memory _accounts_in_use — источник истины).

    Зачем: множество подсистем (warmup, ghost, health, proxy-rotation, monitor)
    фильтруют `in_operation=FALSE`. Если операция умерла/была сброшена сторожем,
    её DB-флаг мог остаться TRUE — аккаунт становится «зомби»: молча выпадает из
    прогрева/призрака/ротации до полного рестарта. Реконсилер лечит это без
    рестарта: чистит флаг там, где память говорит «свободен».

    ПАМЯТЬ ЭТОГО ПРОЦЕССА — НЕ ПОЛНАЯ КАРТИНА. Раньше здесь стоял ровно такой
    вывод, и запрос снимал флаг со всего, чего нет в `_accounts_in_use`. Пока
    процесс один, это верно; со второй репликой (роль web + worker штатно
    разносится) реконсилер видит своё множество и отбирает сессии, которые
    прямо сейчас держит сосед — то есть ведёт ровно к AUTH_KEY_DUPLICATED, от
    которого аренда и защищает. `_db_release` это давно учитывает, реконсилер —
    нет, хотя пишет в ту же колонку.

    Поэтому чистим только то, за что никто живой не отвечает: свою аренду
    (наш зомби-флаг), истёкшую и отсутствующую (legacy-строки до миграции
    аренды). Чужую живую аренду не трогаем — тот же предикат, что на старте в
    reset_stale_in_operation.
    """
    try:
        async with _accounts_lock:
            held = [int(a) for a in _accounts_in_use]
        result = await pool.execute(
            "UPDATE tg_accounts SET in_operation=FALSE, "
            "       op_lease_until=NULL, op_lease_owner=NULL "
            "WHERE COALESCE(in_operation, FALSE)=TRUE "
            "  AND ($1::int[] IS NULL OR id <> ALL($1::int[])) "
            "  AND (op_lease_owner = $2 OR op_lease_owner IS NULL "
            "       OR op_lease_until IS NULL OR op_lease_until < now())",
            held or None, _WORKER_ID,
        )
        freed = int((result or "UPDATE 0").split()[-1])
        if freed:
            log.warning(
                "op_worker reconcile: freed %d zombie in_operation flags "
                "(no live op held them)", freed)
    except Exception as e:
        log_exc_swallow(log, f"op_worker reconcile error: {e}")


# Алертинг о застрявших операциях (наблюдаемость очереди).
_STUCK_PENDING_MIN = 15   # pending дольше N минут = очередь не разбирается
_LONG_RUNNING_MIN = 45    # running дольше N минут = вероятно завис (до reset на 60)
_alerted_stuck_ops: set[int] = set()


async def _watchdog_alerts(pool: asyncpg.Pool, bot: Bot) -> None:
    """Детектирует застрявшие операции и шлёт разовый алерт админам.

    - pending > _STUCK_PENDING_MIN: очередь не разбирается (перегруз/неизвестный op_type)
    - running > _LONG_RUNNING_MIN: вероятно завис (приближается к авто-reset)
    Дедуп по op_id (_alerted_stuck_ops), чтобы не спамить.
    """
    try:
        rows = await pool.fetch(
            """SELECT id, op_type, status, owner_id,
                      EXTRACT(EPOCH FROM (now() - COALESCE(started_at, created_at)))/60 AS age_min
               FROM operation_queue
               WHERE (status='pending'
                        AND created_at < now() - make_interval(mins => $1)
                        -- Отложенные операции (напр. инвайт «продолжим завтра»,
                        -- повтор после cooldown) ЖДУТ scheduled_for и НЕ застряли.
                        -- Поллер их тоже пропускает (scheduled_for <= now()); без
                        -- этого условия каждая продолжающаяся операция сыпала
                        -- ложным алертом «застряла» админам (с чужими owner_id).
                        AND (scheduled_for IS NULL OR scheduled_for <= now()))
                  OR (status='running'  AND started_at < now() - make_interval(mins => $2))
               ORDER BY age_min DESC
               LIMIT 20""",
            _STUCK_PENDING_MIN, _LONG_RUNNING_MIN,
        )
    except Exception as e:
        log_exc_swallow(log, f"op_worker alerts query error: {e}")
        return

    # Активно исполняющиеся в этом процессе running-операции НЕ застряли: они живут
    # в _active_op_ids — тот же критерий, по которому _watchdog_stale их НЕ сбрасывает.
    # Без этой отсечки здоровый долгий mass_invite (пейсинг часами, чтобы не ловить
    # баны) метился «застрял», и админу уходил ложный алерт с чужим owner_id и
    # угрозой «running зависло → будет авто-сброшено» — хотя сброса не будет
    # (операция активна). Тот же класс, что уже закрытый ложняк по отложенным
    # pending: сообщаем только о реально брошенных running (сирота после падения
    # воркера), которые вотчдог и правда сбросит.
    async with _active_lock:
        active_now: frozenset[int] = frozenset(_active_op_ids)
    rows = [
        r for r in rows
        if not (r["status"] == "running" and int(r["id"]) in active_now)
    ]

    fresh = [r for r in rows if int(r["id"]) not in _alerted_stuck_ops]
    if not fresh:
        # подчистим множество от уже завершённых, чтобы не росло бесконечно
        if len(_alerted_stuck_ops) > 500:
            _alerted_stuck_ops.clear()
        return

    for r in fresh:
        _alerted_stuck_ops.add(int(r["id"]))
    log.warning("op_worker: %d stuck operations detected: %s",
                len(fresh), [(int(r["id"]), r["status"]) for r in fresh])

    try:
        from bot.utils.subscription import _admin_ids
        admin_ids = _admin_ids()
    except Exception:
        log.debug('admin_ids fallback: returning empty set')
        admin_ids = set()
    if not admin_ids:
        return

    lines = ["⚠️ <b>Застрявшие операции</b>\n"]
    for r in fresh[:10]:
        icon = "🕐" if r["status"] == "pending" else "⏳"
        lines.append(
            f"{icon} #{r['id']} <code>{r['op_type']}</code> — "
            f"{r['status']} {int(r['age_min'])} мин (owner {r['owner_id']})"
        )
    lines.append("\n<i>pending не разбирается → проверьте воркер/op_type; "
                 "running зависло → будет авто-сброшено.</i>")
    text = "\n".join(lines)
    for aid in admin_ids:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, f"op_worker alert send to {aid} failed")


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Запускается как asyncio.create_task(op_worker.run(pool, bot)) в main.py."""
    log.info("Operation worker started (parallel mode, max=%d)", _MAX_PARALLEL)
    await _reset_stale_running(pool)
    _watchdog_tick = 0
    while True:
        try:
            await _process_pending(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("op_worker error: %s", e)
        _watchdog_tick += 1
        # Heartbeat аренды: пока процесс жив, его аккаунты не уводит другая
        # реплика. Каждые ~30с — на порядок чаще TTL (15 мин), так что одна
        # неудачная попытка ничего не роняет.
        if _watchdog_tick % 3 == 0:
            await renew_leases()
        # Run stale-running watchdog every 6 poll cycles (~1 minute)
        if _watchdog_tick % 6 == 0:
            await _watchdog_stale(pool)
            # Сразу после сброса зависших op снимаем их залипшие in_operation-флаги
            # (аккаунты-«зомби» иначе молча выпадают из warmup/ghost/rotation).
            await _reconcile_in_operation(pool)
        # Alert admins about stuck operations every ~5 minutes (30 cycles)
        if _watchdog_tick % 30 == 0:
            await _watchdog_alerts(pool, bot)
        await asyncio.sleep(_POLL_INTERVAL)


async def shutdown(pool: asyncpg.Pool, grace_s: float = 10.0) -> dict:
    """Свернуть воркер по SIGTERM, не оставив после себя сирот.

    ПОЧЕМУ ЭТО НУЖНО. Railway шлёт SIGTERM на каждом деплое, и до сих пор
    процесс просто закрывал пул поверх работающих операций. Последствий было
    два, и оба видел владелец.

    1. Флот замерзал до 15 минут. Аренда аккаунта (`op_lease_until`) снимается
       либо своим процессом, либо по TTL. Новый процесс чужой арендой не
       считается «своей»: `_WORKER_ID` содержит свежий uuid4, поэтому ветка
       «аренда наша собственная» в reset_stale_in_operation после рестарта НЕ
       срабатывает никогда. Аккаунты оставались `in_operation=TRUE` до
       истечения TTL, а операции, стартовавшие сразу после деплоя, упирались в
       «флот занят» и уходили в отложенный повтор (а через _ACCT_WAIT_MAX_MIN —
       в провал).
    2. Живая операция теряла единицу бюджета живучести. Она оставалась в
       'running', и на старте `_reset_stale_running` считал её жертвой падения:
       revive_count += 1. Три деплоя за время одной многочасовой рассылки — и
       здоровая операция помечалась ядовитой и падала в 'failed', ни разу не
       сломавшись. Бюджет живучести обязан тратиться на операции, которые
       РОНЯЮТ воркер, а не на наши собственные плановые рестарты.

    Поэтому плановая остановка возвращает свои операции в очередь САМА — с
    revive_count нетронутым — и снимает свои аренды сразу. Следующий процесс
    подхватывает работу с чистого листа и со свободным флотом.

    Идемпотентна и никогда не бросает: вызывается из finally при остановке,
    где исключение похоронило бы остаток уборки (закрытие пула и сессии бота).

    Возвращает {"requeued": N, "released": M} — для лога и тестов.
    """
    global _shutting_down
    _shutting_down = True

    async with _active_lock:
        op_ids = sorted(_active_op_ids)
        tasks = [t for t in _active_op_tasks if not t.done()]

    # Даём задачам короткий шанс закончиться самим (исполнители проверяют
    # отмену между целями). Дольше ждать нельзя: платформа убьёт контейнер
    # по своему таймауту, и уборка не успеет отработать вовсе.
    if tasks:
        log.info("op_worker shutdown: жду %d активных операций до %.0fс", len(tasks), grace_s)
        try:
            await asyncio.wait(tasks, timeout=max(0.0, float(grace_s)))
        except Exception as e:
            log_exc_swallow(log, f"op_worker shutdown: ожидание задач: {e}")
        for t in tasks:
            if not t.done():
                t.cancel()

    # Кто успел закончиться — уже в терминальном статусе, и guard ниже их не
    # тронет. Возвращаем в очередь только те, что реально оборвались.
    requeued = 0
    if op_ids:
        res = await _safe_execute(
            pool,
            # revive_count НЕ трогаем: это плановая остановка, а не падение.
            # done_items=0 — как на всех путях возврата в очередь: исполнитель
            # считает прогресс заново, пропуская уже взятые цели по
            # operation_log (иначе класс «done > total»).
            """UPDATE operation_queue
                  SET status='pending', started_at=NULL, done_items=0,
                      last_error=$2
                WHERE id = ANY($1::bigint[]) AND status='running'""",
            [int(i) for i in op_ids],
            "Воркер остановлен на рестарте — операция возвращена в очередь",
            log_ctx="[shutdown_requeue]",
        )
        try:
            requeued = int(str(res).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            requeued = 0

    # Снять СВОИ аренды немедленно, не дожидаясь TTL (см. пункт 1 выше).
    async with _accounts_lock:
        held = [int(a) for a in _accounts_in_use]
        _accounts_in_use.clear()
        _operation_account_locks.clear()
    if held:
        await _db_release(held)

    log.warning(
        "op_worker shutdown: %d операций возвращено в очередь, %d аккаунтов освобождено",
        requeued, len(held),
    )
    return {"requeued": requeued, "released": len(held)}


async def completed_targets(pool: asyncpg.Pool, op_id: int) -> set[str]:
    """Цели операции, уже отработанные УСПЕШНО (operation_log, status='ok').

    Нужно для идемпотентности повторов. Повтор операции — и автоматический
    (_maybe_requeue после FloodWait/сети), и ручной — запускает исполнителя
    заново с done_items=0, и тот проходит ВЕСЬ список целей сначала. Для
    постинга это вторая публикация в канал, который уже получил пост: мусор в
    канале, сожжённый лимит аккаунта и лишний повод для флуда — то есть прямой
    удар по анти-детекту.

    Пустое множество на первом прогоне (журнал ещё пуст), поэтому вызов
    безопасен и там, где повтора не было.

    Ключ — ровно та строка, которую исполнитель пишет в operation_log.target;
    сравнивать имеет смысл только внутри одного исполнителя, где ключ
    канонический (см. комментарий про retry_targets в services/operation_bus.py).
    """
    rows = await _safe_fetch(
        pool,
        "SELECT DISTINCT target FROM operation_log "
        " WHERE op_id=$1 AND status='ok' AND target IS NOT NULL",
        op_id,
        log_ctx=f"[completed_targets op={op_id}]",
    )
    return {str(r["target"]) for r in (rows or []) if r["target"]}


async def completed_account_targets(
    pool: asyncpg.Pool, op_id: int, action: str
) -> set[tuple[int, str]]:
    """Пары (аккаунт, цель), уже отработанные УСПЕШНО в этой операции.

    Пара к `completed_targets`, но для операций, где единица работы — не цель
    сама по себе, а цель В ИСПОЛНЕНИИ КОНКРЕТНОГО АККАУНТА. Массовое вступление
    ведёт в один и тот же канал СО ВСЕХ аккаунтов: пропустить ссылку целиком
    потому, что в канал вошёл аккаунт A, значит не завести туда аккаунт B и
    молча недоделать работу. Поэтому ключ здесь составной.

    ЗАЧЕМ ЭТО НУЖНО. Повтор операции — и автоматический (`_maybe_requeue` после
    сетевой ошибки или флуда), и после сброса зависшей — запускает исполнителя
    заново с done_items=0, и тот проходит ВЕСЬ список целей сначала. Для
    вступления это повторный joinChannel туда, где аккаунт уже состоит: Telegram
    считает его в лимит вступлений и в давление, ведущее к PEER_FLOOD, а
    дневной счётчик аккаунта выгорает на работе, которая уже сделана. То есть
    повтор после сетевого блипа сам по себе поднимал риск бана — ровно то, от
    чего защищает весь пейсинг вокруг.

    Источник — `operation_audit`: там у каждого успешного действия есть и
    operation_id, и account_id, и target, причём пишутся они в той же строке
    кода, что выполняет действие. `operation_log` для этого не годится: в нём
    нет account_id.

    Пустое множество на первом прогоне, поэтому вызов безопасен и там, где
    повтора не было. Никогда не бросает: не смогли прочитать журнал — работаем
    как раньше (лучше лишний повтор, чем сорванная операция).
    """
    rows = await _safe_fetch(
        pool,
        "SELECT DISTINCT account_id, target FROM operation_audit "
        " WHERE operation_id=$1 AND action=$2 AND result='success' "
        "   AND account_id IS NOT NULL AND target IS NOT NULL",
        op_id,
        action,
        log_ctx=f"[completed_account_targets op={op_id}]",
    )
    return {(int(r["account_id"]), str(r["target"])) for r in (rows or [])}


async def _is_cancelled(pool: asyncpg.Pool, op_id: int) -> bool:
    """Check if operation was cancelled by user.

    Uses a 5-second in-memory cache to avoid hammering the DB in tight loops.
    """
    now = time.monotonic()
    cached = _cancel_cache.get(op_id)
    if cached is not None:
        result, checked_at = cached
        if now - checked_at < _CANCEL_CACHE_TTL:
            return result
    row = await _safe_fetchrow(
        pool, "SELECT status FROM operation_queue WHERE id=$1", op_id,
        log_ctx=f"[is_cancelled op={op_id}]",
    )
    result = bool(row and row["status"] == "cancelled")
    _cancel_cache[op_id] = (result, now)
    return result


async def _process_pending(pool: asyncpg.Pool, bot: Bot) -> None:
    if _shutting_down:
        return
    async with _active_lock:
        available_slots = _MAX_PARALLEL - len(_active_op_ids)

    if available_slots <= 0:
        return

    candidate_window = max(
        available_slots * (_MAX_PARALLEL_PER_OWNER + 1), available_slots
    )

    # Атомарно захватить задачи с учетом реальной per-owner параллельности.
    # Иначе лишние задачи одного владельца получают status='running', но фактически
    # стоят внутри semaphore и выглядят для пользователя как зависшие.
    rows = await _safe_fetch(
        pool,
        """WITH owner_running AS (
               SELECT owner_id, COUNT(*)::int AS running_count
               FROM operation_queue
               WHERE status = 'running'
               GROUP BY owner_id
           ),
           pending_locked AS (
               SELECT oq.id,
                      oq.owner_id,
                      oq.created_at,
                      COALESCE(owner_running.running_count, 0) AS running_count
               FROM operation_queue oq
               LEFT JOIN owner_running ON owner_running.owner_id = oq.owner_id
               WHERE oq.status = 'pending'
                 AND (oq.scheduled_for IS NULL OR oq.scheduled_for <= now())
                 AND (oq.requires_approval IS NOT TRUE)
               ORDER BY oq.created_at ASC
               LIMIT $3
               FOR UPDATE OF oq SKIP LOCKED
           ),
           candidates AS (
               SELECT pending_locked.id,
                      pending_locked.owner_id,
                      pending_locked.created_at,
                      ROW_NUMBER() OVER (
                          PARTITION BY pending_locked.owner_id
                          ORDER BY pending_locked.created_at ASC
                      ) AS owner_pending_rank,
                      pending_locked.running_count
               FROM pending_locked
           ),
           picked AS (
               SELECT id
               FROM candidates
               WHERE running_count + owner_pending_rank <= $2
               ORDER BY created_at ASC
               LIMIT $1
           )
           UPDATE operation_queue
           SET status = 'running', started_at = now()
           WHERE id IN (SELECT id FROM picked)
           RETURNING id, owner_id, op_type, params""",
        available_slots,
        _MAX_PARALLEL_PER_OWNER,
        candidate_window,
    )

    for row in rows:
        op_id = row["id"]
        async with _active_lock:
            _active_op_ids.add(op_id)
        _op_task = asyncio.create_task(_run_op_task(pool, bot, dict(row)))
        _active_op_tasks.add(_op_task)
        _op_task.add_done_callback(_active_op_tasks.discard)


# ── Таблица исполнителей операций ────────────────────────────────────────────
# Здесь была цепочка из ~70 веток `elif op_type == ...` внутри _run_op_task.
# Она работала, но делала типы операций НЕАДРЕСУЕМЫМИ: нельзя было спросить у
# модуля «кто исполняет этот op_type» и нельзя было проверить, что каждый
# объявленный в OP_REGISTRY тип вообще исполним — эта связь держалась только на
# том, что автор новой операции не забыл дописать ветку. Теперь соответствие
# «тип → исполнитель» это данные: один словарь, который можно перечислить,
# сверить с реестром (tests/test_op_dispatch.py) и разрезать на драйверы.
#
# Контракт исполнителя: async (pool, bot, op_id, owner_id, params) -> dict.
# Функции с другой сигнатурой оборачиваются адаптером ниже — сигнатура остаётся
# единой, а особенность видна на месте, а не в цепочке условий.
#
# Словарь строится ЛЕНИВО и кэшируется: сами _exec_* определены ниже по файлу,
# и словарь-литерал на этом месте упал бы с NameError при импорте модуля.

_DISPATCH: dict[str, "OpHandler"] | None = None


async def _dispatch_gift_transfer(pool, bot, op_id, owner_id, params) -> dict:
    """Подарки живут в отдельном модуле и берут укороченную сигнатуру."""
    from services.gift_operation import _exec_gift_transfer

    return await _exec_gift_transfer(pool, op_id, params)


async def _dispatch_contacts_sync(pool, bot, op_id, owner_id, params) -> dict:
    """Синхронизация контактов не шлёт сообщений — bot ей не нужен."""
    return await _exec_contacts_sync(pool, op_id, owner_id, params)


async def _dispatch_global_presence_group(pool, bot, op_id, owner_id, params) -> dict:
    """asset_type форсируем: кривые params не должны молча создать канал вместо группы."""
    return await _exec_global_presence_channel(
        pool, bot, op_id, owner_id, {**params, "asset_type": "group"}
    )


async def _dispatch_create_channel(pool, bot, op_id, owner_id, params) -> dict:
    """Channel Factory: один канал через аккаунт. params: {title, about, account_id}."""
    adapted = {**params, "acc_id": params.get("account_id"),
               "prefix": params.get("title"), "count": 1, "is_group": False}
    return await _exec_bulk_create_channels(pool, bot, op_id, owner_id, adapted)


async def _dispatch_create_group(pool, bot, op_id, owner_id, params) -> dict:
    """Group Factory: одна группа через аккаунт. params: {title, account_id, is_supergroup}."""
    adapted = {**params, "acc_id": params.get("account_id"),
               "prefix": params.get("title"), "count": 1, "is_group": True}
    return await _exec_bulk_create_channels(pool, bot, op_id, owner_id, adapted)


def _build_dispatch() -> dict:
    """Собрать таблицу «op_type → исполнитель». Вызывается один раз, лениво."""
    return {
        # ── Массовые операции по каналам и ботам ──────────────────────────────
        "mass_publish": _exec_mass_publish,
        "bulk_bot_edit": _exec_bulk_bot_edit,
        "bulk_join": _exec_bulk_join,
        "bulk_leave": _exec_bulk_leave,
        "find_contact": _exec_find_contact,
        "bulk_create_channels": _exec_bulk_create_channels,
        "check_channel_rankings": _exec_check_channel_rankings,
        "bot_factory": _exec_bot_factory,
        "bulk_edit_channels": _exec_bulk_edit_channels,
        "bulk_seo_apply": _exec_bulk_seo_apply,
        "bulk_post_to_channel": _exec_bulk_post_to_channel,
        "bulk_chan_exec": _exec_bulk_chan_exec,
        "bulk_post_chans": _exec_bulk_post_chans,
        "pin_last_post": _exec_pin_last_post,
        "channel_import_all": _exec_channel_import_all,
        "channel_add": _exec_channel_add,
        "reclassify_channels": _exec_reclassify_channels,
        "promote_all_admins": _exec_promote_all_admins,
        "crosspost_run": _exec_crosspost_run,
        # ── Global Presence ───────────────────────────────────────────────────
        # Пакет = сначала каналы; бот создаётся отдельной операцией по плану.
        "global_presence_channel": _exec_global_presence_channel,
        "global_presence_package": _exec_global_presence_channel,
        "global_presence_full_package": _exec_global_presence_channel,
        "global_presence_group": _dispatch_global_presence_group,
        "global_presence_bot": _exec_global_presence_bot,
        "gp_bulk_apply": _exec_gp_bulk_apply,
        "seed_presence_pack": _exec_seed_presence_pack,
        "promote_presence_pack": _exec_promote_presence_pack,
        "deploy_network": _exec_deploy_network,
        # ── Группы и сообщества ───────────────────────────────────────────────
        "group_import_all": _exec_group_import_all,
        "group_announce": _exec_group_announce,
        "community_add_channel": _exec_community_add_channel,
        "community_liven": _exec_community_liven,
        "community_set_staff": _exec_community_set_staff,
        # ── Аккаунты ──────────────────────────────────────────────────────────
        "bulk_update_profile": _exec_bulk_update_profile,
        "check_accounts_health": _exec_check_accounts_health,
        "scan_owned_resources": _exec_scan_owned_resources,
        "scan_owned_bots": _exec_scan_owned_bots,
        "connect_discovered_bots": _exec_connect_discovered_bots,
        "enable_bot_to_bot": _exec_enable_bot_to_bot,
        "account_warmup": _exec_account_warmup,
        "auto_register": _exec_auto_register,
        "leave_all_chats": _exec_leave_all_chats,
        "read_all_dialogs": _exec_read_all_dialogs,
        "delete_private_dialogs": _exec_delete_private_dialogs,
        "delete_contacts": _exec_delete_contacts,
        "reg_check": _exec_reg_check,
        "phone_check": _exec_phone_check,
        # bulk_set_profile — единственная реализация; profile_setter её псевдоним.
        "bulk_set_profile": _exec_bulk_set_profile,
        "profile_setter": _exec_bulk_set_profile,
        # ── Рассылки и охват ──────────────────────────────────────────────────
        "dm_campaign": _exec_dm_campaign,
        "network_broadcast": _exec_network_broadcast,
        "bulk_dm_adhoc": _exec_bulk_dm_adhoc,
        "run_broadcast": _exec_run_broadcast,
        "mass_invite": _exec_mass_invite,
        "create_chatlist_folder": _exec_create_chatlist_folder,
        "self_promo_blast": _exec_self_promo_blast,
        # ── Накрутка ──────────────────────────────────────────────────────────
        "boost_views": _exec_boost_views,
        "boost_reactions": _exec_boost_reactions,
        "boost_stories": _exec_boost_stories,
        "boost_subscribers": _exec_boost_subscribers,
        "boost_bot_starts": _exec_boost_bot_starts,
        # ── Контент и ИИ ──────────────────────────────────────────────────────
        "ai_comment": _exec_ai_comment,
        "content_clone": _exec_content_clone,
        "clone_adapt": _exec_clone_adapt,
        "niche_growth_post": _exec_niche_growth_post,
        # quick_post переиспользует mass_publish: тот уже умеет явный channel_ids.
        "quick_post": _exec_mass_publish,
        # ── Разведка, жалобы, подарки ─────────────────────────────────────────
        "strike": _exec_strike,
        "mass_report": _exec_mass_report,
        "report_peer": _exec_report_peer,
        "compliance_scan": _exec_compliance_scan,
        "ad_intel_scan": _exec_ad_intel_scan,
        "parse_audience": _exec_parse_audience,
        "gift_scan": _exec_gift_scan,
        "gift_transfer": _dispatch_gift_transfer,
        # ── Прочее ────────────────────────────────────────────────────────────
        "contacts_sync": _dispatch_contacts_sync,
        "create_channel": _dispatch_create_channel,
        "create_group": _dispatch_create_group,
    }


def dispatch_table() -> dict:
    """Таблица «op_type → исполнитель» (ленивая сборка, кэш на модуль)."""
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _build_dispatch()
    return _DISPATCH


def handler_for(op_type: str):
    """Исполнитель для типа операции или None, если тип неизвестен воркеру."""
    return dispatch_table().get(op_type)


async def _notify_recurring_stopped(
    pool: asyncpg.Pool, bot: Bot, owner_id: int, op_id: int, op_type: str, why: str
) -> None:
    """Сказать владельцу, что повторяющаяся операция больше не продлевается.

    Расписание, оборвавшееся молча, — худший вид отказа: канал просто перестаёт
    наполняться, и владелец узнаёт об этом через неделю, если заметит вообще.
    Best-effort: уведомление не должно ронять завершение операции.
    """
    try:
        await db.notify_if_enabled(
            pool, bot, owner_id, "op_complete",
            f"⏹ <b>Повтор операции #{op_id}</b> (<code>{op_type}</code>) остановлен: {why}.\n\n"
            f"Следующий запуск по расписанию не поставлен. "
            f"Запустите операцию заново, когда ограничение снимется.",
        )
    except Exception:
        log_exc_swallow(log, f"op_worker: уведомление об остановке расписания op#{op_id}")


async def _run_op_task(pool: asyncpg.Pool, bot: Bot, row: dict) -> None:
    """Запустить одну операцию в отдельной asyncio-задаче."""
    op_id = row["id"]
    owner_id = row["owner_id"]
    op_type = row["op_type"]
    # Трассировка (аудит №6): операция бежит в своей asyncio-задаче, поэтому
    # контекст корреляции здесь изолирован. Проставляем его в НАЧАЛЕ — тогда
    # каждая строка лога этой операции несёт cid/op_id/user_id, и разбор аварии
    # («логи запуска как ты просил») перестаёт быть склейкой вручную. Инфра уже
    # была (services.logger), но фоновые операции её не выставляли.
    from services.logger import set_correlation_id, generate_correlation_id
    set_correlation_id(
        correlation_id=generate_correlation_id(),
        user_id=owner_id,
        op_id=f"op{op_id}",
    )
    _t_started = time.monotonic()      # метрика длительности операции (аудит №6)
    params = (
        row["params"]
        if isinstance(row["params"], dict)
        else json.loads(row["params"] or "{}")
    )

    # Skip operations waiting for user approval
    if row.get("requires_approval") and row.get("status") == "waiting_approval":
        async with _active_lock:
            _active_op_ids.discard(op_id)
        return

    # Circuit breaker: skip if owner is in cooldown
    # ОБЩЕЕ состояние: пауза, поставленная соседней репликой, обязана остановить
    # и нас — иначе предохранитель не предохраняет ничего.
    _cb_now = await circuit_breaker_status(owner_id)
    if _cb_now.get("status") == "open":
        remaining = _cb_now.get("cooldown_remaining_s", 0) or 0
        log.warning(
            "op_worker: circuit breaker OPEN for owner=%d op=%d — pausing %ds",
            owner_id, op_id, remaining,
        )
        # Вернуть операцию в очередь с отложенным повтором, иначе она зависнет
        # в 'running' до stale-watchdog (60 мин) и займёт слот параллельности.
        try:
            await _release_op_for_circuit(pool, op_id, remaining)
        except Exception:
            log_exc_swallow(log, "op_worker: circuit release failed op=%d", op_id)
        async with _active_lock:
            _active_op_ids.discard(op_id)
        return

    # Получить семафор для ограничения параллельных операций на одного владельца
    try:
        owner_sem = await _get_owner_semaphore(owner_id)
    except Exception:
        log.exception(
            "op_worker: failed to get semaphore for owner=%d op=%d", owner_id, op_id
        )
        async with _active_lock:
            _active_op_ids.discard(op_id)
        return

    progress_task: asyncio.Task | None = None
    _t_start = time.monotonic()
    log.info(
        "op_worker: starting op_id=%d op_type=%s owner=%d", op_id, op_type, owner_id
    )

    async with owner_sem:
        try:
            # Уведомить пользователя о старте
            try:
                from aiogram.utils.keyboard import InlineKeyboardBuilder
                from bot.callbacks import BmCb

                start_kb = InlineKeyboardBuilder()
                start_kb.button(
                    text="📋 Очередь операций", callback_data=BmCb(action="op_reports")
                )
                # Pre-flight оценка успеха по истории (Behavioral 6B) — показываем
                # только при достаточной уверенности, best-effort.
                _pred_line = ""
                try:
                    from services import behavioral_engine as _be
                    _pred = await _be.predict_campaign_success(pool, owner_id, op_type)
                    if _pred.get("confidence") in ("medium", "high"):
                        _pred_line = (
                            f"\nОжидаемый успех: ~{int(_pred.get('success_rate', 0))}% "
                            f"(по истории, {_pred.get('confidence')})"
                        )
                except Exception:
                    _pred_line = ""
                await db.notify_if_enabled(
                    pool,
                    bot,
                    owner_id,
                    "op_complete",
                    f"⚙️ <b>Операция #{op_id}</b> запущена: <code>{op_type}</code>{_pred_line}",
                    reply_markup=start_kb.as_markup(),
                )
            except Exception:
                log_exc_swallow(
                    log, f"Сбой отправки уведомления о запуске операции #{op_id}"
                )

            # Праймим per-owner политику прокси в кэш account_manager, чтобы
            # массовые исполнители (словари аккаунтов несут owner_id) применяли
            # выбор владельца strict/allow_direct, а не только процессный дефолт.
            try:
                from services import account_manager as _am
                _am.set_owner_proxy_policy(owner_id, await db.get_proxy_policy(pool, owner_id))
                # И IPv6-подсеть — по тем же причинам, но цена ошибки выше.
                # Кэш подсети заполняется ТОЛЬКО при сохранении настройки, то есть
                # в процессе, обслужившем запрос мини-аппа. Воркер о ней не знает
                # и уводит аккаунт НАПРЯМУЮ с host-IP, тогда как одиночные вызовы
                # (они читают подсеть из БД) идут с IPv6 аккаунта. Одна сессия с
                # двух адресов — это AUTH_KEY_DUPLICATED, то есть мёртвый аккаунт,
                # а снаружи «операция ничего не делает».
                # ..._or_raise: сбой чтения НЕ должен выглядеть как «подсети нет» —
                # иначе он же и уведёт аккаунт напрямую. Не прочитали — оставляем
                # кэш как есть.
                _am.set_owner_ipv6_subnet(
                    owner_id, await db.get_ipv6_subnet_or_raise(pool, owner_id))
                # И карту транспорта аккаунтов — по той же причине и с той же
                # ценой ошибки. Массовые исполнители ходят по собственным
                # выборкам, в которых почти нигде нет cf_relay_url: без карты
                # аккаунт с релеем уйдёт в операции НАПРЯМУЮ, а одиночные вызовы
                # (канонический запрос) — через релей. Два адреса на одну
                # сессию — AUTH_KEY_DUPLICATED.
                await _am.prime_account_transport(pool, owner_id)
            except Exception:
                log_exc_swallow(log, f"prime proxy_policy op#{op_id}")

            # Гидрируем flood-state владельца из БД (аудит #5): после рестарта
            # процесса накопленная осторожность по флудам (cooldown/риск) должна
            # восстановиться, а не начинаться с нуля базовым темпом.
            try:
                from services import flood_engine as _fe
                _acc_ids = [int(r["id"]) for r in (await _safe_fetch(
                    pool, "SELECT id FROM tg_accounts WHERE owner_id=$1", owner_id) or [])]
                await _fe.hydrate_states(pool, _acc_ids)
            except Exception:
                log_exc_swallow(log, f"hydrate flood-state op#{op_id}")

            # Запустить фоновый монитор прогресса для длинных операций.
            # Обслуживающие op-типы (contacts_sync) НЕ мониторим — иначе поминутный
            # спам «0%» по одной и той же операции.
            if op_type not in _QUIET_PROGRESS_OPS:
                progress_task = asyncio.create_task(
                    _progress_monitor(pool, bot, op_id, owner_id, op_type)
                )

            # Диспетчеризация по таблице (см. _build_dispatch выше): раньше здесь
            # стояла цепочка из ~70 `elif op_type == ...`.
            _handler = handler_for(op_type)
            if _handler is None:
                log.warning(
                    "op_worker: unknown op_type=%r for op_id=%s owner_id=%s — marking failed",
                    op_type,
                    op_id,
                    owner_id,
                )
                result = {
                    "status": "failed",
                    "reason": f"unknown op_type: {op_type}",
                    "summary": f"⚠️ Неизвестный тип операции: {op_type}",
                }
            else:
                # Локальный импорт — как и остальные обращения к шине в этом
                # файле (operation_bus тянет подписки и иммунитет, модульный
                # импорт замкнул бы цикл).
                from services import operation_bus as _obus_t

                _timeout_s = _obus_t.timeout_for(op_type, _OP_TIMEOUT_DEFAULT_S)
                try:
                    result = await asyncio.wait_for(
                        _handler(pool, bot, op_id, owner_id, params),
                        timeout=_timeout_s,
                    )
                except asyncio.TimeoutError as _to:
                    # Громко: зависшая операция раньше не оставляла следа вообще.
                    log.error(
                        "op_worker: op_id=%d op_type=%s превысила потолок %dс — "
                        "прогон прерван, аккаунты и слот освобождаются",
                        op_id, op_type, _timeout_s,
                    )
                    raise TimeoutError(
                        f"Операция не уложилась в {_timeout_s // 60} мин и была "
                        f"прервана (вероятно, зависла на ответе Telegram или прокси)"
                    ) from _to

            # Флот временно занят другими операциями — не проваливаем, а мягко
            # возвращаем в очередь (запустится, когда освободятся аккаунты).
            if result.get("status") == "requeue":
                # defer_s задаётся, когда задержку назначил не дефицит флота, а
                # внешнее ограничение (флуд-пауза Telegram). У таких возвратов
                # свой путь: _requeue_op_no_accounts проваливает операцию, если
                # та ждёт дольше _ACCT_WAIT_MAX_MIN от создания, а ждать час,
                # который назначил Telegram, — штатное поведение, а не сбой.
                _defer_s = result.get("defer_s")
                if _defer_s:
                    await _defer_op_for_flood(
                        pool, op_id, int(_defer_s), result.get("reason") or "")
                else:
                    await _requeue_op_no_accounts(pool, op_id)
                return

            # Не перезаписывать статус если операция была отменена в процессе
            if result.get("status") == "cancelled":
                await _safe_execute(
                    pool,
                    "UPDATE operation_queue SET status='cancelled', finished_at=now() "
                    f"WHERE id=$1 AND status NOT IN {op_status.sql_terminal_list()}",
                    op_id,
                    log_ctx=f"[run_op_cancelled op={op_id}]",
                )
                return

            current = await _safe_fetchrow(
                pool,
                "SELECT status FROM operation_queue WHERE id=$1",
                op_id,
                log_ctx=f"[run_op_check_cancel op={op_id}]",
            )
            if current and current["status"] == "cancelled":
                await _safe_execute(
                    pool,
                    "UPDATE operation_queue SET finished_at=now() "
                    "WHERE id=$1 AND status='cancelled' AND finished_at IS NULL",
                    op_id,
                    log_ctx=f"[run_op_cancelled_finish op={op_id}]",
                )
                return

            elapsed = time.monotonic() - _t_start
            duration_seconds = round(elapsed, 1)
            result = _normalize_result(result, op_type, duration_seconds)
            # Честный финальный статус — решает services/op_status.classify_final
            # по РЕАЛЬНЫМ счётчикам, а не по слову исполнителя.
            #
            # История класса. Сначала success-путь жёстко писал 'done', из-за
            # чего операция выглядела выполненной, даже когда исполнитель вернул
            # status='failed' или когда все элементы провалились. Это чинилось
            # здесь же ветвлением done/failed — и осталась вторая половина той же
            # лжи: операция, взявшая 203 цели из 380 со 177 ошибками, попадала в
            # ветку 'done' и уходила владельцу с зелёной галочкой. Недоведённая
            # работа теперь имеет собственный терминальный статус 'partial'.
            _ok = int(result.get("ok", 0) or 0)
            _failed = int(result.get("failed", 0) or 0)
            _progress = await _safe_fetchrow(
                pool,
                "SELECT done_items, total_items FROM operation_queue WHERE id=$1",
                op_id,
                log_ctx=f"[run_op_progress op={op_id}]",
            )
            _final_status = op_status.classify_final(
                result.get("status"),
                ok=_ok,
                failed=_failed,
                done_items=(_progress["done_items"] if _progress else None),
                total_items=(_progress["total_items"] if _progress else None),
            )
            # result["status"] обязан совпадать с тем, что легло в очередь: его
            # читают отчёты, мини-апп и кнопка повтора. Разъехавшись, они
            # показывали разный исход одной операции.
            result["status"] = _final_status
            log.info(
                "op_worker: op_id=%d op_type=%s → %s in %.1fs (ok=%d failed=%d) — %s",
                op_id, op_type, _final_status, elapsed, _ok, _failed,
                result.get("summary", ""),
            )
            # Circuit breaker: считаем ТОЛЬКО реальные сбои исполнения.
            #
            # Предохранитель существует, чтобы остановить владельца, у которого
            # операции ломаются на ходу (аккаунты умирают, сеть, Telegram). Но
            # исполнители возвращают status='failed' и на чисто КОНФИГУРАЦИОННЫХ
            # отказах — «аудитория пуста», «не указана группа», «нет активных
            # аккаунтов». Таких возвратов в op_worker десятки.
            #
            # Из-за этого три подряд неверно заполненные формы (например запуск
            # инвайта, когда парсер ещё не собирал аудиторию) открывали цепь — и
            # ВСЕ операции владельца на 30 минут начинали молча откладываться.
            # Снаружи это выглядит как «ничего не работает»: операция уходит в
            # «ожидает» и не стартует, причём без единого объяснения.
            #
            # Признак конфигурационного отказа: исполнитель не тронул НИ ОДНОЙ
            # цели (ok=0 и failed=0) и не бросил исключение — то есть он отказал
            # ДО работы, а не сломался в ней. Исключения учитываются отдельно,
            # ниже по коду, и предохранителя не теряют.
            _config_refusal = _final_status == "failed" and _ok == 0 and _failed == 0
            if _config_refusal:
                log.info(
                    "op_worker: op_id=%d — отказ до начала работы (%s), "
                    "предохранитель не трогаем",
                    op_id, (result.get("summary") or "")[:80],
                )
            else:
                # partial считаем успехом ДЛЯ ПРЕДОХРАНИТЕЛЯ: цепь существует,
                # чтобы остановить владельца, у которого операции ломаются
                # целиком. Операция, взявшая часть целей, — доказательство
                # обратного: Telegram нас пускает. Открыв цепь на партиалах, мы
                # бы глушили ровно те массовые операции, которым по их природе
                # положено терять часть целей (инвайт, DM).
                await _circuit_breaker_record(owner_id, op_status.is_productive(_final_status))
            # Adaptive pacing: record result for learning
            _productive = op_status.is_productive(_final_status)
            session_simulator.record_success(op_type, 0, elapsed) if _productive else session_simulator.record_failure(op_type, 0, elapsed)
            # ML pacing engine: feed success/failure so get_multiplier() learns
            try:
                get_pacing_engine().record_result(
                    success=_productive, action_type=op_type
                )
            except Exception as e:
                log.warning("pacing_engine record_result failed for op %d: %s", op_id, e)
            # Честное зеркало: при «мягком» провале (исполнитель вернул
            # status='failed' с reason/summary, без исключения) error_msg НЕ
            # заполнялся, а operation_status читает summary/error_msg → пользователь
            # видел «Ошибка» без причины. Пишем error_msg из reason/summary.
            _err_text = None
            if _final_status in ("failed", op_status.PARTIAL):
                _err_text = (str(result.get("reason") or result.get("summary") or "").strip()[:300]) or None
            await _safe_execute(
                pool,
                "UPDATE operation_queue SET status=$3, finished_at=now(), result=$1::jsonb, "
                "error_msg=COALESCE($4, error_msg) "
                f"WHERE id=$2 AND status NOT IN {op_status.sql_terminal_list()}",
                json.dumps(result, ensure_ascii=False),
                op_id,
                _final_status,
                _err_text,
                log_ctx=f"[run_op_done op={op_id}]",
            )
            # Метрики (аудит №6): доля успеха и длительность по типу операции —
            # без них о деградации узнавали из жалобы клиента, а не с графика.
            try:
                from services import metrics as _m
                _m.inc("infragram_operations_total",
                       {"op_type": op_type, "status": _final_status})
                _m.observe("infragram_operation_seconds",
                           time.monotonic() - _t_started, {"op_type": op_type})
            except Exception:
                pass
            # Событие в шину организма: операция завершена (память + реакция мозга).
            try:
                from services.organism import spine
                await spine.emit(pool, owner_id, "op_done", {
                    "op_id": op_id, "op_type": op_type, "status": _final_status,
                    "ok": result.get("ok"), "failed": result.get("failed"),
                    "summary": (result.get("summary") or "")[:300]})
            except Exception:
                pass
            # Комплаенс: подписанная запись в аудит-трейл — покрываем ВСЕ операции
            # (единый choke point), а не выборочно. record() никогда не бросает.
            try:
                from services import compliance_engine
                await compliance_engine.record(
                    pool, owner_id, None, op_type, _final_status,
                    op_id=op_id, params=params if isinstance(params, dict) else None)
            except Exception:
                pass
            # Autopost v2: рекуррентная переочередь постинг-операций в СВОИ каналы.
            # repeat_interval_min>0 → после успешного прогона ставим следующий с
            # scheduled_for=now()+interval. Только постинг-op'ы (allowlist) — чтобы
            # аккаунт-действия не зациклились. repeat_count (если задан) декрементим.
            # partial тоже продлевает расписание: иначе один сбойный канал в
            # серии навсегда обрывал автопостинг — операция закрывалась не
            # «done», следующий запуск не ставился, и владелец узнавал об этом
            # только по тишине в канале.
            if op_status.is_productive(_final_status) and op_type in _RECURRING_OK_OPS:
                try:
                    _rmin = int(params.get("repeat_interval_min") or 0)
                except (TypeError, ValueError):
                    _rmin = 0
                if _rmin > 0:
                    _rcount = params.get("repeat_count")
                    _go = True
                    _next_params = dict(params)
                    if _rcount is not None:
                        try:
                            _rc = int(_rcount) - 1
                        except (TypeError, ValueError):
                            _rc = 0
                        _next_params["repeat_count"] = _rc
                        _go = _rc > 0
                    if _go:
                        # Следующий круг ставится ЧЕРЕЗ ШИНУ, а не прямым INSERT.
                        # Прямая запись обходила предохранитель Ban Weather: если
                        # этот тип операции прямо сейчас массово убивает аккаунты
                        # владельца, расписание всё равно заводило новый круг —
                        # расписание оказывалось сильнее защиты, ради которой
                        # предохранитель и существует. Обходился и гейт тарифа:
                        # автопостинг, заведённый на платном плане, продолжал
                        # работать вечно после отмены подписки.
                        from services import operation_bus as _obus_r
                        import datetime as _dt_r

                        _row = await _safe_fetchrow(
                            pool,
                            "SELECT label, total_items FROM operation_queue WHERE id=$1",
                            op_id, log_ctx=f"[recurring_label op={op_id}]")
                        _base_label = (_row["label"] if _row else None) or op_type
                        # Метку повтора ставим один раз: без этой проверки она
                        # копилась кругами («Пост ↻↻↻↻») и к сотому запуску
                        # вытесняла из очереди само название.
                        _next_label = _base_label if _base_label.endswith(" ↻") else _base_label + " ↻"
                        try:
                            _next_id = await _obus_r.submit(
                                pool, owner_id, op_type, _next_params,
                                total_items=(_row["total_items"] if _row else 0),
                                scheduled_for=_dt_r.datetime.now(_dt_r.timezone.utc)
                                + _dt_r.timedelta(minutes=_rmin),
                                label=_next_label,
                            )
                            log.info(
                                "autopost v2: op=%d → следующий круг op=%d через %d мин",
                                op_id, _next_id, _rmin,
                            )
                        except _obus_r.ImmunityBlockedError as _ie:
                            # Не сбой: предохранитель намеренно не пускает этот
                            # тип операции. Круг пропущен, расписание оборвано —
                            # владелец должен узнать об этом, а не по тишине в
                            # канале.
                            log.warning(
                                "autopost v2: op=%d — следующий круг НЕ поставлен, "
                                "предохранитель: %s", op_id, _ie,
                            )
                            await _notify_recurring_stopped(
                                pool, bot, owner_id, op_id, op_type,
                                f"предохранитель приостановил этот тип операций: {_ie}")
                        except _obus_r.PlanRequiredError as _pe:
                            log.warning(
                                "autopost v2: op=%d — следующий круг НЕ поставлен, "
                                "тариф: %s", op_id, _pe,
                            )
                            await _notify_recurring_stopped(
                                pool, bot, owner_id, op_id, op_type,
                                "у подписки больше нет доступа к этому типу операций")
                        except Exception as _e:
                            log.warning("autopost v2: reschedule failed op=%d: %s", op_id, _e)
            # Audit trail: write operation completion to operation_audit.
            # Outcome согласован с финальным статусом — провал не пишется как success.
            _audit_outcome = {
                op_status.DONE: "success",
                op_status.PARTIAL: "partial",
            }.get(_final_status, "failed")
            _op_summary = result.get("summary", "")
            _acc_ids_done = params.get("account_ids") or []
            if _acc_ids_done:
                for _audit_acc_id in _acc_ids_done:
                    await _audit(
                        pool,
                        owner_id,
                        op_type,
                        _audit_outcome,
                        operation_id=op_id,
                        account_id=int(_audit_acc_id),
                        duration_ms=int(duration_seconds * 1000),
                    )
            else:
                await _audit(
                    pool,
                    owner_id,
                    op_type,
                    _audit_outcome,
                    operation_id=op_id,
                    duration_ms=int(duration_seconds * 1000),
                )

            # Physics Engine + Compliance telemetry (fire-and-forget с удержанием
            # ссылки — класс 14; compliance-запись = аудит-след, терять нельзя).
            try:
                from services import physics_engine as _pe
                from services import compliance_engine as _ce
                from services.bg_tasks import spawn
                _dur_ms = int(duration_seconds * 1000)
                _outcome = result.get("status", "success")
                _comp_outcome = "success" if _outcome == "done" else _outcome
                if _acc_ids_done:
                    for _tid in _acc_ids_done:
                        spawn(
                            _pe.record_telemetry(
                                pool, int(_tid), owner_id, op_type, _audit_outcome, 0, _dur_ms
                            )
                        )
                spawn(
                    _ce.record(pool, owner_id, None, op_type, _comp_outcome, op_id)
                )
            except Exception as e:
                log_exc_swallow(log, f"physics/compliance telemetry failed for op {op_id}: {e}")

            summary = _op_summary
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from bot.callbacks import BmCb, StrikeCb, MassPubCb, MassOpCb
            from services import operation_bus

            kb = InlineKeyboardBuilder()
            # Partial-fail → retry: повторить ТОЛЬКО неудавшиеся цели, а не всю
            # операцию. Перезапуск целиком тратит лимиты аккаунтов и повторно
            # обрабатывает успешные цели (дубли постов, повторные вступления).
            #
            # mass_publish имеет собственный обработчик (умеет переносить медиа);
            # для остальных типов кнопка появляется, только если op_type объявил
            # retry_targets — то есть operation_log.target у него однозначно
            # обратим в цель. Молча угадывать нельзя: повтор ушёл бы не туда.
            if op_type == "mass_publish" and _failed > 0:
                kb.button(
                    text=f"🔁 Повторить неудавшиеся ({_failed})",
                    callback_data=MassPubCb(action="retry_failed", target_id=op_id),
                )
            elif _failed > 0 and operation_bus.supports_retry_failed(op_type):
                kb.button(
                    text=f"🔁 Повторить неудавшиеся ({_failed})",
                    callback_data=MassOpCb(action="retry_targets", op_id=op_id),
                )
            kb.button(
                text="📋 Детали операции",
                callback_data=BmCb(action="op_detail", op_id=op_id),
            )
            # For strike operations add a direct shortcut to strike history
            if op_type == "strike":
                kb.button(
                    text="📜 История Strike",
                    callback_data=StrikeCb(action="history"),
                )
            kb.adjust(1)
            # Strike summaries can be very long (one block per target × many accounts).
            # Telegram messages cap at 4096 chars; truncate to leave room for the header.
            _notify_icon = op_status.icon(_final_status)
            _notify_verb = op_status.label(_final_status)
            _notify_header = f"{_notify_icon} <b>Операция #{op_id}</b> {_notify_verb} за {duration_seconds}с\n"
            _max_summary = 4096 - len(_notify_header) - 50
            _summary_notify = summary[:_max_summary] + ("…" if len(summary) > _max_summary else "")
            await db.notify_if_enabled(
                pool,
                bot,
                owner_id,
                "op_complete",
                _notify_header + _summary_notify,
                reply_markup=kb.as_markup(),
            )
            # Фиксируем исход в Infrastructure Memory для всех аккаунтов из params
            try:
                from services.infra_memory import record_account_op

                _mem_ok = op_status.is_productive(_final_status)
                for _acc_id in params.get("account_ids") or []:
                    record_account_op(
                        int(_acc_id), op_type, success=_mem_ok, duration_s=duration_seconds
                    )
            except Exception as e:
                log_exc_swallow(log, f"infra_memory record_account_op (success) failed for op {op_id}: {e}")

            # Memory Feedback Loop: mark linked intent as completed
            try:
                from database import db as _db

                intent_row = await _db.get_intent_by_op(pool, op_id)
                if intent_row:
                    await _db.update_intent_status(
                        pool, intent_row["id"], intent_row["owner_id"], "completed"
                    )
                    await _db.save_intent_feedback(
                        pool,
                        intent_row["id"],
                        intent_row["owner_id"],
                        {
                            "op_id": op_id,
                            "actual_done": result.get("ok", 0),
                            "actual_duration_s": duration_seconds,
                            "op_type": op_type,
                        },
                    )
            except Exception as _ie:
                log.debug("intent feedback link: %s", _ie)

        except Exception as e:
            log.exception("op_worker: op %d failed: %s", op_id, e)
            # Немедленно деактивировать аккаунт при AUTH_KEY/SESSION_REVOKED
            await _deactivate_dead_session(pool, e, params)
            # Фиксируем ошибку в Infrastructure Memory
            try:
                from services.infra_memory import record_account_op

                for _acc_id in params.get("account_ids") or []:
                    record_account_op(
                        int(_acc_id), op_type, success=False, error=str(e)[:100]
                    )
            except Exception as e2:
                log_exc_swallow(log, f"infra_memory record_account_op (error) failed for op {op_id}: {e2}")
            # Попытаться поставить на повтор перед тем как помечать как failed
            requeued = await _maybe_requeue(pool, op_id, e, params, op_type)
            if not requeued:
                _fail_res = await _safe_execute(
                    pool,
                    "UPDATE operation_queue SET status='failed', finished_at=now(), error_msg=$1 "
                    f"WHERE id=$2 AND status NOT IN {op_status.sql_terminal_list()}",
                    str(e)[:500],
                    op_id,
                    log_ctx=f"[run_op_failed op={op_id}]",
                )
                # Ноль строк = операция уже в терминальном статусе, и почти
                # всегда это ОТМЕНА владельцем: он нажал «Отменить», исполнитель
                # в этот момент оборвался, и мы пришли сюда с исключением.
                # Отмену не переписываем и не докладываем о ней как об ошибке —
                # иначе владелец получает «❌ завершилась с ошибкой» на то, что
                # сам только что остановил.
                if str(_fail_res or "").strip().endswith(" 0"):
                    log.info(
                        "op_worker: op=%d уже в терминальном статусе (вероятно отмена) "
                        "— провал не записываем", op_id,
                    )
                    return
                # Circuit breaker: record failure
                await _circuit_breaker_record(owner_id, False)
                # ML pacing engine: feed failure with flood/ban granularity
                try:
                    _kind = _classify_op_error(e)
                    get_pacing_engine().record_result(
                        success=False,
                        is_flood=_kind in ("flood", "peer_flood"),
                        is_ban=_kind == "fatal",
                        action_type=op_type,
                    )
                except Exception as e3:
                    log.warning("pacing_engine record_result (error) failed for op %d: %s", op_id, e3)
                # Audit trail: write final failure to operation_audit for all related accounts
                _err_str = str(e)[:400]
                _acc_ids_for_audit = params.get("account_ids") or []
                if _acc_ids_for_audit:
                    for _audit_acc_id in _acc_ids_for_audit:
                        await _audit(
                            pool,
                            owner_id,
                            op_type,
                            "failed",
                            operation_id=op_id,
                            account_id=int(_audit_acc_id),
                            error_msg=_err_str,
                        )
                else:
                    # No account_ids — write one audit entry with no account_id
                    await _audit(
                        pool,
                        owner_id,
                        op_type,
                        "failed",
                        operation_id=op_id,
                        error_msg=_err_str,
                    )
                from aiogram.utils.keyboard import InlineKeyboardBuilder
                from bot.callbacks import BmCb

                kb = InlineKeyboardBuilder()
                kb.button(
                    text="📋 Детали операции",
                    callback_data=BmCb(action="op_detail", op_id=op_id),
                )
                retry_row = await _safe_fetchrow(
                    pool,
                    "SELECT retry_count, max_retries FROM operation_queue WHERE id=$1",
                    op_id,
                    log_ctx=f"[run_op_retry_info op={op_id}]",
                )
                retry_info = ""
                if retry_row:
                    rc = retry_row["retry_count"] or 0
                    mr = retry_row["max_retries"] or 3
                    if rc > 0:
                        retry_info = f"\nПопыток: {rc}/{mr} — лимит исчерпан"
                    else:
                        retry_info = "\nОшибка не повторяется (фатальная)"
                await db.notify_if_enabled(
                    pool,
                    bot,
                    owner_id,
                    "op_complete",
                    f"❌ <b>Операция #{op_id}</b> завершилась с ошибкой:\n"
                    f"<code>{str(e)[:200]}</code>{retry_info}\n\n"
                    f"💡 Используйте кнопку «Повторить» или проверьте аккаунты.",
                    reply_markup=kb.as_markup(),
                )

        finally:
            if progress_task and not progress_task.done():
                progress_task.cancel()
            await release_operation_accounts(op_id)
            elapsed_total = time.monotonic() - _t_start
            duration_seconds_total = round(elapsed_total, 1)
            log.info(
                "op_worker: op_id=%d op_type=%s finished (total %.1fs, duration_seconds=%.1f)",
                op_id,
                op_type,
                elapsed_total,
                duration_seconds_total,
            )
            async with _active_lock:
                _active_op_ids.discard(op_id)


async def _exec_bulk_bot_edit(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выполнить массовое редактирование ботов через Bot API."""
    field = params.get("field", "")
    value = params.get("value", "")

    from database.db import fetch_bots as _fetch_bots_op
    bots_rows = await _fetch_bots_op(
        pool,
        "SELECT id, token FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
        owner_id,
    )
    if not bots_rows:
        return {"status": "failed", "summary": "⚠️ Bulk Bot Edit: нет активных ботов"}

    ok_count = 0
    fail_count = 0
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", len(bots_rows), op_id
    )

    field_to_method = {
        "name": "setMyName",
        "desc": "setMyDescription",
        "short_desc": "setMyShortDescription",
        "commands": "setMyCommands",
    }
    method = field_to_method.get(field)
    if not method:
        return {"status": "skipped", "reason": f"Unknown field: {field}"}

    # Parse commands for field=commands: "/cmd - description" format
    commands_payload: list | None = None
    if field == "commands":
        commands_payload = []
        for line in (value or "").strip().splitlines():
            line = line.strip()
            if " - " in line:
                cmd_part, desc_part = line.split(" - ", 1)
                cmd = cmd_part.strip().lstrip("/")
                if cmd:
                    commands_payload.append(
                        {"command": cmd, "description": desc_part.strip()[:256]}
                    )

    async with aiohttp.ClientSession() as sess:
        for b in bots_rows:
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "failed": fail_count,
                    "summary": f"Отменено. Обновлено: {ok_count}, ошибок: {fail_count}",
                }
            try:
                if field == "commands":
                    resp = await sess.post(
                        f"https://api.telegram.org/bot{b['token']}/{method}",
                        json={"commands": commands_payload or []},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                else:
                    param_key = (
                        "name"
                        if field == "name"
                        else "description"
                        if field == "desc"
                        else "short_description"
                    )
                    resp = await sess.post(
                        f"https://api.telegram.org/bot{b['token']}/{method}",
                        json={param_key: value},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                data_resp = await resp.json()
                if data_resp.get("ok"):
                    ok_count += 1
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                        op_id,
                        ok_count + fail_count,
                        str(b["id"]),
                    )
                else:
                    fail_count += 1
                    log.warning(
                        "op_worker bulk_bot_edit: bot=%s field=%s api_error=%s",
                        b.get("id"),
                        field,
                        data_resp.get("description"),
                    )
            except Exception as e:
                fail_count += 1
                log.warning(
                    "op_worker bulk_bot_edit: bot=%s field=%s error=%s",
                    b.get("id"),
                    field,
                    e,
                )
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            await asyncio.sleep(1)

    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "summary": f"Обновлено: {ok_count} ботов, ошибок: {fail_count}",
    }


async def _exec_dm_campaign(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Execute a DM campaign via dm_engine.run_campaign().

    Params:
      campaign_id (int) — id from dm_campaigns table.

    Progress is tracked in dm_campaigns table (sent_count/total_targets/status).
    The operation_queue entry tracks op-level completion only.
    Cancellation: setting dm_campaigns.status='paused' causes dm_engine inner loop to stop.
    """
    campaign_id = int(params.get("campaign_id", 0))
    if not campaign_id:
        return {
            "status": "failed",
            "summary": "⚠️ DM Campaign: campaign_id missing in params",
        }

    # Verify campaign exists and belongs to this owner
    campaign = await _safe_fetchrow(
        pool,
        "SELECT id, name, status, owner_id FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
        campaign_id,
        owner_id,
        log_ctx=f"[dm_campaign_check op={op_id}]",
    )
    if not campaign:
        return {
            "status": "failed",
            "summary": f"⚠️ DM Campaign #{campaign_id} not found or wrong owner",
        }

    if await _is_cancelled(pool, op_id):
        # Cancelled before starting — mark campaign paused so user can resume later
        await _safe_execute(
                pool,
            "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
        )
        return {"status": "cancelled", "summary": "Operation cancelled before start"}

    from services.dm_engine import run_campaign

    try:
        await run_campaign(pool, bot, campaign_id, op_id=op_id)
    except asyncio.CancelledError:
        # op_worker cancelled this asyncio task — mark campaign paused
        try:
            await pool.execute(
                "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
            )
        except Exception as e:
            log.warning("dm_campaign pause on cancel failed for campaign %d: %s", campaign_id, e)
        raise

    # Read final counts from dm_campaigns for the completion summary
    final = await _safe_fetchrow(
            pool,
        "SELECT status, sent_count, fail_count, total_targets FROM dm_campaigns WHERE id=$1",
        campaign_id,
    )
    if final:
        sent = final["sent_count"] or 0
        failed = final["fail_count"] or 0
        total = final["total_targets"] or 0
        name = campaign["name"] or f"#{campaign_id}"
        # Кампанию могли поставить на паузу (кнопкой или дневным лимитом) —
        # тогда операция «выполнена», но рассылка НЕ завершена. Раньше в панели
        # операций это выглядело одинаково с честным завершением.
        _paused = final["status"] == "paused"
        _tail = " · ⏸ на паузе, можно возобновить" if _paused else ""
        summary = (f"📨 DM «{name}»: ✅ {sent} отправлено, ❌ {failed} ошибок, "
                   f"📊 {total} всего{_tail}")
        return {
            "status": "done",
            "ok": sent,
            "failed": failed,
            "total": total,
            "paused": _paused,
            "summary": summary,
        }
    return {"status": "done", "summary": f"📨 DM campaign #{campaign_id} completed"}


async def _exec_mass_publish(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Опубликовать сообщение во все управляемые каналы/группы владельца."""
    from services import account_manager

    target = params.get("target", "channels")
    mp_text = str(params.get("text") or params.get("mp_text") or "").strip()

    # Content safety backstop: запрещённый контент (CSAM / терроризм) не публикуется.
    try:
        from services import content_safety

        _v = content_safety.scan_text(mp_text)
        if _v.blocked:
            log.warning(
                "_exec_mass_publish op=%d BLOCKED by content_safety: category=%s rule=%s",
                op_id, _v.category, _v.rule,
            )
            try:
                from services import compliance_engine
                await compliance_engine.record(
                    pool, owner_id, None,
                    op_type="content_block:mass_publish",
                    outcome="blocked", op_id=op_id,
                    params={"category": _v.category, "rule": _v.rule},
                )
            except Exception as e:
                log.warning("compliance_engine record (content_block) failed for op %d: %s", op_id, e)
            return {"status": "failed", "summary": "🚫 Публикация заблокирована: запрещённый контент"}
    except Exception as _cs_err:
        log.debug("_exec_mass_publish content_safety check failed: %s", _cs_err)

    delay = int(params.get("delay_seconds") or params.get("delay") or 30)
    explicit_channel_ids = [int(i) for i in (params.get("channel_ids") or [])]
    # Optional media attachment (from Quick Post Wizard step 3)
    media_file_id: str | None = params.get("media_file_id") or None
    media_type: str | None = params.get("media_type") or None
    # media_bytes: downloaded once before the loop, re-uploaded per channel via Telethon.
    # Bot API file_ids cannot be used directly by Telethon (different protocol),
    # so we download the file bytes first using the main bot token.
    media_bytes: bytes | None = None
    media_filename: str = "media"
    if media_file_id and media_type:
        try:
            tg_file = await bot.get_file(media_file_id)
            file_url = tg_file.file_path
            if file_url and not file_url.startswith("http"):
                from config import BOT_TOKEN
                file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_url}"
            if file_url:
                import aiohttp as _aiohttp
                async with _aiohttp.ClientSession() as _sess:
                    async with _sess.get(file_url, timeout=_aiohttp.ClientTimeout(total=60)) as _resp:
                        if _resp.status == 200:
                            media_bytes = await _resp.read()
                            # Derive filename from path
                            import os as _os
                            media_filename = _os.path.basename(file_url) or "media"
                        else:
                            log.warning(
                                "_exec_mass_publish op=%d: media download failed status=%d, posting text-only",
                                op_id, _resp.status,
                            )
        except Exception as _media_exc:
            log.warning(
                "_exec_mass_publish op=%d: failed to download media (%s), posting text-only",
                op_id, _media_exc,
            )
            media_bytes = None

    # Brand injection: free-tier users get @MEXAHI3MBOT appended to every channel post
    try:
        from services import brand_injection as _bi
        if await _bi.is_user_free_tier(pool, owner_id):
            mp_text = _bi.add_promo(mp_text, html=True)
    except Exception as e:
        log_exc_swallow(log, f"mass_publish brand_injection failed for op {op_id}: {e}")

    if not mp_text:
        return {"status": "failed", "summary": "⚠️ Текст сообщения не указан"}

    if target == "channels":
        type_filter = "(mc.type = 'channel' OR mc.type IS NULL)"
    elif target == "groups":
        type_filter = "mc.type IN ('megagroup', 'supergroup', 'group', 'chat')"
    else:
        type_filter = "TRUE"

    explicit_acc_ids = [int(i) for i in (params.get("account_ids") or [])]
    accounts_raw = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=explicit_acc_ids or None,
        action_type="mass_publish",
        respect_daily_budget=True,  # долговечность: не грузим исчерпавшие лимит аккаунты
    )
    if not accounts_raw:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов"}

    accounts_rows = await _claim_available_accounts(op_id, accounts_raw, owner_id)
    mp_used_acc_ids = [int(a["id"]) for a in accounts_rows]

    if not accounts_rows:
        return {
            "status": "requeue",
            "summary": "⏳ Mass Publish: флот занят другими операциями — операция в очереди",
        }

    # Account health awareness: filter out banned/restricted accounts before publishing
    try:
        from services import account_health as _ah

        healthy_acc_ids: set[int] = set()
        for _acc in accounts_rows:
            _h = _ah.get_health(_acc["id"])
            if _h.health_score >= 10.0:  # exclude only completely dead accounts
                healthy_acc_ids.add(_acc["id"])
        if healthy_acc_ids != {a["id"] for a in accounts_rows}:
            excluded = len(accounts_rows) - len(healthy_acc_ids)
            log.warning(
                "_exec_mass_publish op=%d: excluded %d unhealthy accounts",
                op_id,
                excluded,
            )
            accounts_rows = [a for a in accounts_rows if a["id"] in healthy_acc_ids]
    except Exception:
        log_exc_swallow(
            log,
            f"_exec_mass_publish op={op_id}: health check failed, using all accounts",
        )

    # Риск-пульс (Волна S/1B, fail-open): дополнительно отсеиваем аккаунты с
    # недавним СЕРЬЁЗНЫМ ограничением (restriction_events) — второй иммунный
    # сигнал помимо account_health.health_score. Публикация с флагнутого аккаунта
    # = быстрый бан. Пустой результат НЕ обнуляет операцию (лучше рискнуть).
    try:
        _kept = []
        for _acc in accounts_rows:
            if not await _infra_mem.is_account_quarantined(pool, _acc["id"]):
                _kept.append(_acc)
        if _kept and len(_kept) != len(accounts_rows):
            log.info("_exec_mass_publish op=%d: пропущено %d аккаунтов в карантине",
                     op_id, len(accounts_rows) - len(_kept))
            accounts_rows = _kept
    except Exception:
        log_exc_swallow(log, f"_exec_mass_publish op={op_id}: quarantine check failed")

    acc_ids = [a["id"] for a in accounts_rows]
    chan_filter = (
        "AND mc.channel_id = ANY($3::bigint[])" if explicit_channel_ids else ""
    )
    fetch_params: list = [owner_id, acc_ids]
    if explicit_channel_ids:
        fetch_params.append(explicit_channel_ids)
    db_pairs = await _safe_fetch(
            pool,
        f"SELECT "
        f"mc.channel_id AS id, mc.title, mc.username, mc.access_hash, mc.type, "
        f"a.id AS acc_id, a.session_str, a.first_name, a.phone, "
        f"a.device_model, a.system_version, a.app_version, "
        f"a.lang_code, a.system_lang_code, a.cf_relay_url, a.proxy_id, p.proxy_url, p.geo_country "
        f"FROM managed_channels mc "
        f"JOIN tg_accounts a ON a.id = mc.acc_id AND a.is_active = TRUE AND a.session_str IS NOT NULL "
        f"LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
        f"WHERE mc.owner_id = $1 AND mc.acc_id = ANY($2::bigint[]) AND {type_filter} {chan_filter} "
        f"ORDER BY mc.channel_id, a.id",
        *fetch_params,
    )

    if not db_pairs:
        await release_accounts(mp_used_acc_ids)
        return {
            "status": "done",
            "ok": 0,
            "failed": 0,
            "summary": "Нет каналов для рассылки",
        }

    acc_map = {a["id"]: dict(a) for a in accounts_rows}
    target_map: dict[int, dict] = {}
    for row in db_pairs:
        acc = acc_map.get(row["acc_id"])
        if not acc:
            continue
        channel_id = int(row["id"])
        if channel_id not in target_map:
            target_map[channel_id] = {
                "dialog": {
                    "id": row["id"],
                    "title": row["title"],
                    "username": row["username"] or "",
                    "access_hash": row["access_hash"] or 0,
                    "type": row["type"] or "channel",
                    "username": row["username"] or "",
                },
                "accounts": [],
            }
        target_map[channel_id]["accounts"].append(acc)

    targets = list(target_map.values())
    total = len(targets)
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
    )

    # Pre-scan: for channels without access_hash AND without username,
    # do one full dialog scan per account to resolve missing hashes before the main loop.
    # Persists resolved hashes to DB so future runs use the fast path (Strategy 1).
    _needs_hash = [t for t in targets if not t["dialog"]["access_hash"] and not t["dialog"].get("username")]
    if _needs_hash:
        _prescan_accs: dict[int, dict] = {}
        for _t in _needs_hash:
            for _a in _t["accounts"]:
                if _a["id"] not in _prescan_accs:
                    _prescan_accs[_a["id"]] = _a
        log.info("mass_publish op=%d: pre-scanning %d accounts for %d channels without access_hash",
                 op_id, len(_prescan_accs), len(_needs_hash))
        for _pa in _prescan_accs.values():
            try:
                _all_dlg = await account_manager.get_dialogs(_pa["session_str"], limit=None, _acc=_pa) or []
                _dlg_map: dict[int, int] = {
                    int(d["id"]): int(d["access_hash"])
                    for d in _all_dlg
                    if d.get("access_hash")
                }
                for _t in targets:
                    _d = _t["dialog"]
                    _cid = int(_d["id"])
                    if not _d["access_hash"] and _cid in _dlg_map:
                        _d["access_hash"] = _dlg_map[_cid]
                        try:
                            await pool.execute(
                                "UPDATE managed_channels SET access_hash=$1 "
                                "WHERE owner_id=$2 AND channel_id=$3 AND (access_hash IS NULL OR access_hash=0)",
                                _dlg_map[_cid], owner_id, _cid,
                            )
                        except Exception as e:
                            log.warning("mass_publish pre-scan: persist access_hash failed for ch=%s: %s", _cid, e)
            except asyncio.CancelledError:
                raise
            except Exception as _pe:
                log.warning("mass_publish op=%d: pre-scan failed for acc=%s: %s", op_id, _pa.get("id"), _pe)

    ok_count = 0
    fail_count = 0
    failed_channels: list[str] = []
    published_to: list[str] = []
    isolated_accounts: set[int] = set()
    # Spintax НА КАНАЛ: одинаковый текст во все каналы разом — палевная сигнатура
    # координации (Telegram это ловит). Если в тексте есть {A|B} — каждый канал
    # получает свой вариант; без spintax expand возвращает текст как есть (no-op).
    from services.dm_engine import expand_spintax as _expand_spintax

    # Идемпотентность повтора. Повтор (автоматический после FloodWait/сети или
    # ручной) запускает исполнителя заново с done_items=0, и он проходит ВЕСЬ
    # список каналов сначала — включая те, куда пост уже ушёл. Это вторая
    # публикация в канал: мусор у подписчиков, сожжённый лимит аккаунта и лишний
    # повод для флуда. На первом прогоне журнал пуст, поэтому множество пустое и
    # поведение не меняется.
    _already_published = await completed_targets(pool, op_id)
    if _already_published:
        log.info(
            "mass_publish op=%d: повтор — %d каналов уже опубликованы, пропускаем",
            op_id, len(_already_published),
        )

    for idx, target_entry in enumerate(targets, 1):
        dialog = target_entry["dialog"]
        if str(dialog["id"]) in _already_published:
            # Считаем успехом: цель операции достигнута на прошлом прогоне, и
            # итоговые счётчики должны описывать ВСЮ операцию, а не только
            # добор. Иначе повтор отчитывался бы «1 из 60» на выполненной работе.
            ok_count += 1
            await _safe_execute(
                pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )
            continue
        candidate_accounts = [
            _acc
            for _acc in target_entry["accounts"]
            if _acc["id"] not in isolated_accounts
        ]
        acc = candidate_accounts[0] if candidate_accounts else None
        if await _is_cancelled(pool, op_id):
            await release_accounts(mp_used_acc_ids)
            return {
                "status": "cancelled",
                "ok": ok_count,
                "failed": fail_count,
                "failed_channels": failed_channels[:50],
                "summary": f"Отменено. Опубликовано: {ok_count}, ошибок: {fail_count}",
            }
        if acc is None:
            # Изолирован аккаунт ТОЛЬКО этого канала — пропускаем канал, но НЕ
            # обрываем операцию: остальные каналы управляются ДРУГИМИ аккаунтами
            # (mc.acc_id), многие здоровы. Каналы отсортированы по channel_id, т.е.
            # перемешаны по аккаунтам. Раньше здесь было fail_count+=remaining; break
            # — один изолированный аккаунт валил ВСЕ оставшиеся каналы, включая
            # управляемые здоровыми аккаунтами (симптом «3 успеха / 56 ошибок»).
            fail_count += 1
            ch_label = str(dialog.get("title") or dialog["id"])[:60]
            if ch_label not in failed_channels:
                failed_channels.append(ch_label)
            await _safe_execute(
                    pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'error',$4)",
                op_id,
                idx,
                str(dialog["id"]),
                "Аккаунт временно изолирован после сетевого/прокси сбоя",
            )
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )
            continue
        flood_wait = 0
        _published = False
        last_error = ""
        # Use @username as channel ref if access_hash is 0 (faster than iter_dialogs)
        _ch_ref = (
            f"@{dialog['username']}"
            if not dialog["access_hash"] and dialog.get("username")
            else dialog["id"]
        )
        _ch_text = _expand_spintax(mp_text)  # свой вариант текста на этот канал
        for _attempt in range(2):  # per-item retry: 1 initial + 1 retry on FloodWait
            try:
                result = await account_manager.post_to_channel(
                    acc["session_str"],
                    _ch_ref,
                    _ch_text,
                    access_hash=dialog["access_hash"],
                    username=dialog.get("username") or "",
                    _acc=acc,
                    media_bytes=media_bytes,
                    media_type=media_type,
                    media_filename=media_filename,
                )
                if result.get("proxy_error"):
                    raise ConnectionError(
                        str(result.get("error", "proxy/network error"))
                    )
                if "error" in result or result.get("banned"):
                    raise Exception(str(result.get("error", "publish error")))
                ok_count += 1
                _published = True
                _ch_title = str(dialog.get("title") or dialog.get("username") or dialog["id"])[:60]
                published_to.append(_ch_title)
                _infra_mem.record_account_op(acc["id"], "publish", success=True)
                # Сигнал активности канала: реальная публикация → last_post_at.
                # Кормит ecosystem auto_remove_dead_channels и аналитику (раньше
                # такого сигнала не было нигде). Best-effort — не рушим публикацию.
                try:
                    await pool.execute(
                        "UPDATE managed_channels SET last_post_at=now() "
                        "WHERE owner_id=$1 AND channel_id=$2",
                        owner_id, int(dialog["id"]),
                    )
                except Exception:
                    log_exc_swallow(log, "mass_publish: last_post_at update")
                # target = channel_id, как и в error-ветке ниже. Раньше успех
                # писал ЗАГОЛОВОК канала, а провал — id: сопоставить «уже
                # опубликовано» с «упало» было нечем, поэтому и повтор, и
                # проверка идемпотентности работали вслепую. Заголовок не
                # теряется — он уходит в message (экран деталей печатает
                # «target — message»).
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok',$4)",
                    op_id, idx, str(dialog["id"]), _ch_title,
                )
                await _audit(
                    pool,
                    owner_id,
                    "publish",
                    "success",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=_ch_title,
                )
                try:
                    from services.flood_engine import record_success

                    await record_success(acc["id"], "publish")
                except Exception:
                    log_exc_swallow(log, "mass_publish: record_success failed")
                # Persist resolved access_hash so future publishes use fast path
                _resolved_hash = result.get("resolved_access_hash", 0)
                if _resolved_hash and not dialog.get("access_hash"):
                    try:
                        await pool.execute(
                            "UPDATE managed_channels SET access_hash=$1 "
                            "WHERE owner_id=$2 AND channel_id=$3 AND (access_hash IS NULL OR access_hash=0)",
                            _resolved_hash, owner_id, int(dialog["id"]),
                        )
                        dialog["access_hash"] = _resolved_hash
                    except Exception as e:
                        log_exc_swallow(log, f"mass_publish: persist resolved access_hash failed for ch={dialog['id']}: {e}")
                break  # success — stop retry loop
            except Exception as e:
                err_str = str(e)[:200]
                last_error = err_str
                flood_wait = extract_flood_wait(e, err_str)
                if _is_network_or_proxy_error(err_str) or _is_dead_session_error(err_str):
                    isolated_accounts.add(acc["id"])
                    if _is_dead_session_error(err_str):
                        # Dead session — deactivate account immediately
                        try:
                            await pool.execute(
                                """UPDATE tg_accounts
                                   SET is_active=FALSE, acc_status='session_expired',
                                       status_reason=$2
                                   WHERE id=$1 AND is_active=TRUE""",
                                acc["id"],
                                f"Dead session (mass_publish): {err_str[:180]}",
                            )
                            log.warning(
                                "mass_publish: deactivated dead session account_id=%d: %s",
                                acc["id"], err_str[:100],
                            )
                        except Exception:
                            log_exc_swallow(log, "mass_publish: dead session deactivate failed")
                    else:
                        try:
                            await _record_network_isolation(
                                pool,
                                acc["id"],
                                "publish",
                                op_id,
                                err_str,
                            )
                        except Exception:
                            log_exc_swallow(log, "mass_publish: network isolation failed")
                    for fallback_acc in candidate_accounts[1:]:
                        if fallback_acc["id"] in isolated_accounts:
                            continue
                        acc = fallback_acc
                        try:
                            fallback_result = await account_manager.post_to_channel(
                                fallback_acc["session_str"],
                                _ch_ref,
                                _ch_text,
                                access_hash=dialog["access_hash"],
                                username=dialog.get("username") or "",
                                _acc=fallback_acc,
                                media_bytes=media_bytes,
                                media_type=media_type,
                                media_filename=media_filename,
                            )
                            if fallback_result.get("proxy_error"):
                                raise ConnectionError(
                                    str(
                                        fallback_result.get(
                                            "error", "proxy/network error"
                                        )
                                    )
                                )
                            if "error" in fallback_result or fallback_result.get(
                                "banned"
                            ):
                                raise Exception(
                                    str(
                                        fallback_result.get(
                                            "error", "publish fallback error"
                                        )
                                    )
                                )
                            acc = fallback_acc
                            ok_count += 1
                            _published = True
                            _infra_mem.record_account_op(
                                acc["id"], "publish", success=True
                            )
                            await _audit(
                                pool,
                                owner_id,
                                "publish",
                                "success",
                                operation_id=op_id,
                                account_id=acc["id"],
                                target=str(dialog.get("title") or dialog["id"])[:100],
                            )
                            try:
                                from services.flood_engine import record_success

                                await record_success(acc["id"], "publish")
                            except Exception:
                                log_exc_swallow(
                                    log, "mass_publish: record_success failed"
                                )
                            break
                        except Exception as fallback_exc:
                            err_str = str(fallback_exc)[:200]
                            last_error = err_str
                            if _is_network_or_proxy_error(err_str) or _is_dead_session_error(err_str):
                                isolated_accounts.add(fallback_acc["id"])
                                if _is_dead_session_error(err_str):
                                    try:
                                        await pool.execute(
                                            """UPDATE tg_accounts
                                               SET is_active=FALSE, acc_status='session_expired',
                                                   status_reason=$2
                                            WHERE id=$1 AND is_active=TRUE""",
                                            fallback_acc["id"],
                                            f"Dead session (mass_publish fallback): {err_str[:160]}",
                                        )
                                    except Exception as e:
                                        log_exc_swallow(log, f"mass_publish: fallback dead session deactivate failed: {e}")
                                else:
                                    try:
                                        await _record_network_isolation(
                                            pool,
                                            fallback_acc["id"],
                                            "publish",
                                            op_id,
                                            err_str,
                                        )
                                    except Exception:
                                        log_exc_swallow(
                                            log,
                                            "mass_publish: fallback network isolation failed",
                                        )
                                continue
                            break
                    break
                if flood_wait and _attempt == 0:
                    # FloodWait on first attempt — sleep and retry once
                    log.warning(
                        "mass_publish: FloodWait %ds on %s, retrying once",
                        flood_wait,
                        dialog.get("title") or dialog["id"],
                    )
                    try:
                        from services.flood_engine import record_flood

                        await record_flood(
                            pool, acc["id"], flood_wait, "publish", op_id
                        )
                    except Exception:
                        log_exc_swallow(log, "mass_publish: record_flood failed")
                    if flood_wait > _FLOOD_INLINE_MAX_S:
                        # Длинную паузу пересиживать нельзя: прогон держал бы
                        # слот и арендованные аккаунты часами, ничего не делая.
                        # Откладываем операцию целиком — она возобновится, когда
                        # пауза истечёт, и пропустит уже опубликованные каналы
                        # (completed_targets), так что дублей не будет.
                        await release_accounts(mp_used_acc_ids)
                        return {
                            "status": "requeue",
                            "defer_s": flood_wait + 60,
                            "reason": (
                                f"Telegram назначил паузу {flood_wait // 60} мин — "
                                f"операция продолжится автоматически"
                            ),
                            "ok": ok_count,
                            "failed": fail_count,
                        }
                    await asyncio.sleep(flood_wait + random.uniform(2, 8))
                    continue  # retry
                # Non-retryable failure or second attempt failed
                break

        if not _published:
            fail_count += 1
            err_str = (last_error or "unknown error")[:200]
            ch_label = str(dialog.get("title") or dialog["id"])[:60]
            if ch_label not in failed_channels:
                failed_channels.append(ch_label)
            _infra_mem.record_account_op(
                acc["id"], "publish", success=False, error=err_str[:100]
            )
            await _audit(
                pool,
                owner_id,
                "publish",
                "flood_wait" if flood_wait else "error",
                operation_id=op_id,
                account_id=acc["id"],
                target=ch_label,
                error_msg=err_str[:200],
                flood_wait_s=flood_wait if flood_wait else None,
            )
            if flood_wait:
                try:
                    from services.flood_engine import record_flood

                    await record_flood(pool, acc["id"], flood_wait, "publish", op_id)
                except Exception:
                    log_exc_swallow(log, "mass_publish: record_flood failed")
            await _safe_execute(
                    pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'error',$4)",
                op_id,
                idx,
                str(dialog["id"]),
                err_str,
            )

        await _safe_execute(
                pool,
            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
        )
        if delay > 0 and idx < total:
            if flood_wait:
                # Flood-пауза мандатная (Telegram) — не масштабируем губернатором,
                # но и не даём ей остановить весь прогон на часы: следующая цель
                # идёт через другой канал и часто через другой аккаунт.
                await bounded_flood_sleep(
                    max(delay, float(flood_wait) + 5), "mass_publish")
            else:
                # Базовый темп — под глобальным губернатором (давление флота).
                await _governed_sleep(pool, owner_id, delay)

    await release_accounts(mp_used_acc_ids)

    # Обновляем прогресс цели в growth_goals (delta = успешных публикаций)
    goal_id_param = params.get("goal_id")
    if goal_id_param and ok_count > 0:
        try:
            await pool.execute(
                "UPDATE growth_goals SET current_value = current_value + $2, updated_at=NOW() WHERE id=$1",
                int(goal_id_param), ok_count,
            )
        except Exception as e:
            log.warning("mass_publish: update growth_goals failed for goal %s: %s", goal_id_param, e)

    parts = [f"Опубликовано: {ok_count}", f"ошибок: {fail_count}"]
    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "failed_channels": failed_channels[:50],
        "published_to": published_to[:50],
        "summary": ", ".join(parts),
    }


async def _exec_bulk_join(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Вступить в список каналов/групп несколькими аккаунтами."""
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    accounts = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=account_ids or None,
        action_type="join",
        respect_daily_budget=True,  # долговечность: щадим выжатые аккаунты
    )

    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    used_acc_ids = [int(a["id"]) for a in accounts]

    if not accounts:
        return {
            "status": "requeue",
            "summary": "⏳ Bulk Join: флот занят другими операциями — операция в очереди",
        }

    try:
        return await _exec_bulk_join_inner(pool, bot, op_id, owner_id, params, accounts)
    finally:
        await release_accounts(used_acc_ids)


async def _exec_bulk_join_inner(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict, accounts
) -> dict:
    from services import account_manager
    from services.flood_engine import (
        gaussian_delay,
        recommended_delay,
        record_peer_flood,
    )

    links = params.get("links") or params.get("targets") or []
    delay_mode = params.get("delay_mode", "smart")
    ok_count = 0
    fail_count = 0
    step = 0
    skipped_by_limit = 0
    failed_links: list[str] = []
    _JOIN_DAY_LIMITS = {"fast": 20, "normal": 15, "slow": 8, "smart": 12}
    day_limit = _JOIN_DAY_LIMITS.get(delay_mode, 12)

    if not links:
        return {
            "status": "done",
            "ok": 0,
            "failed": 0,
            "skipped_accounts": 0,
            "failed_links": [],
            "summary": "Список ссылок пуст — нечего выполнять.",
        }

    # proxy_mode: "bound" (default) = use account's bound proxy
    #             "relay" = strip proxy, force CF relay for all accounts
    proxy_mode = params.get("proxy_mode", "bound")

    total_steps = len(links) * len(accounts)
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total_steps, op_id
    )

    # Аккаунты, изолированные из-за сбоя bound-прокси (смена IP ломает auth key).
    # Объявляем до цикла — иначе NameError при первом же proxy_error.
    isolated_accounts: set[int] = set()

    # Идемпотентность повтора. Операция перезапускается целиком — после сетевой
    # ошибки (_maybe_requeue), после флуд-паузы, после сброса зависшей. Без этой
    # выборки каждый такой перезапуск снова вступал в каналы, где аккаунт уже
    # состоит: Telegram считает повторный joinChannel в лимит вступлений и в
    # давление, ведущее к PEER_FLOOD, а дневной счётчик аккаунта выгорал на уже
    # сделанной работе. Повтор после блипа сети сам поднимал риск бана.
    _already_joined = await completed_account_targets(pool, op_id, "join")
    if _already_joined:
        log.info(
            "bulk_join op=%d: повтор прогона, пропускаю %d уже выполненных пар "
            "(аккаунт, ссылка)", op_id, len(_already_joined),
        )

    for acc_idx, acc in enumerate(accounts):
        if acc["id"] in isolated_accounts:
            continue
        if proxy_mode == "relay":
            # Strip bound proxy — Telethon will use CF relay instead
            acc_dict = {**dict(acc), "proxy_url": None, "enforce_proxy": False}
        else:
            acc_dict = dict(acc)
        try:
            joins_today = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_audit "
                "WHERE account_id=$1 AND action='join' AND result='success' "
                "AND occurred_at > NOW() - INTERVAL '24 hours'",
                acc["id"],
            )
        except Exception:
            log.debug('joins_today query failed, defaulting to 0')
            joins_today = 0
        if (joins_today or 0) >= day_limit:
            log.info(
                "bulk_join: аккаунт %s достиг дневного лимита join (%d), пропуск",
                acc_dict.get("phone"),
                day_limit,
            )
            skipped_by_limit += 1
            continue
        # Риск-пульс (Волна S/1B, fail-open): аккаунт с недавним СЕРЬЁЗНЫМ
        # ограничением не трогаем в массовой операции — уводим от риска повторного
        # бана. Нет сигнала/ошибка → работаем как раньше (не блокируем ядро).
        if await _infra_mem.is_account_quarantined(pool, acc["id"]):
            log.info(
                "bulk_join: аккаунт %s в карантине (недавнее ограничение), пропуск",
                acc_dict.get("phone"),
            )
            skipped_by_limit += 1
            continue
        for i, link in enumerate(links):
            # Уже вступали этим аккаунтом в эту ссылку на прошлом прогоне —
            # цель закрыта, второй joinChannel только жжёт лимит и риск.
            # Считаем её успешной: работа сделана, и отчёт обязан это отражать,
            # иначе владелец увидит «выполнено 12 из 50» на полностью
            # доведённой операции.
            if (int(acc["id"]), str(link)) in _already_joined:
                ok_count += 1
                step += 1
                await _safe_execute(
                    pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                    log_ctx=f"[bulk_join_skip_done op={op_id}]",
                )
                continue
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "failed": fail_count,
                    "skipped_accounts": skipped_by_limit,
                    "failed_links": failed_links[:50],
                    "summary": f"Отменено. Вступлено: {ok_count}, ошибок: {fail_count}",
                }
            step += 1
            t0 = time.monotonic()
            flood_wait = 0
            try:
                res = await account_manager.join_channel(
                    acc["session_str"], link, _acc=acc_dict
                )
                # Proxy error → do NOT retry with different IP (causes auth key collision).
                # Instead, mark account as proxy-broken and skip remaining links.
                if res.get("proxy_error") and proxy_mode == "bound":
                    # Изолируем аккаунт (транспорт-клиент переиспользуется на все ссылки —
                    # после сбоя подключения следующие ссылки всё равно упадут так же).
                    # НО причину даём честную: аккаунту без назначенного прокси нельзя
                    # писать «исправьте прокси» — это уводит чинить несуществующее.
                    _logmsg, _usermsg = _proxy_skip_reason(acc)
                    log.warning("bulk_join: acc=%s %s", acc_dict.get("phone", "?"), _logmsg)
                    await _safe_execute(
                        pool,
                        "INSERT INTO operation_log(op_id, step_num, target, status, message)"
                        " VALUES($1,$2,$3,'error',$4)",
                        op_id, step, link, _usermsg,
                        log_ctx=f"[bulk_join_proxy_skip op={op_id}]",
                    )
                    isolated_accounts.add(acc["id"])
                    break  # stop all remaining links for this account
                # peer_flood=True means account-level join rate-limit (PEER_FLOOD).
                # This is NOT a channel ban — apply a cooldown and skip remaining
                # links for this account to avoid escalation to a real spamblock.
                if res.get("peer_flood"):
                    fail_count += 1
                    _peer_flood_wait = 48 * 3600
                    err_str = res.get("error", "PeerFlood")[:200]
                    log.warning(
                        "op_worker bulk_join: PEER_FLOOD on acc=%s — cooldown %ds, skipping remaining links",
                        acc_dict.get("phone"),
                        _peer_flood_wait,
                    )
                    try:
                        await record_peer_flood(
                            pool,
                            acc["id"],
                            action_type="join",
                            operation_id=op_id,
                            cooldown_seconds=_peer_flood_wait,
                        )
                    except Exception:
                        log_exc_swallow(
                            log,
                            f"Сбой записи PeerFlood в flood_engine для аккаунта {acc['id']}",
                        )
                    _infra_mem.record_account_op(
                        acc["id"], "join", success=False, error="PeerFlood"
                    )
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                        "VALUES($1,$2,$3,'error',$4)",
                        op_id,
                        step,
                        link,
                        err_str,
                    )
                    await _audit(
                        pool,
                        owner_id,
                        "join",
                        "peer_flood",
                        operation_id=op_id,
                        account_id=acc["id"],
                        target=link,
                        error_msg=err_str,
                        flood_wait_s=_peer_flood_wait,
                    )
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                        op_id,
                    )
                    break  # stop all remaining links for this account
                if res.get("error"):
                    raise Exception(str(res["error"]))
                ok_count += 1
                dur_ms = int((time.monotonic() - t0) * 1000)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok','joined')",
                    op_id,
                    step,
                    link,
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
                await _audit(
                    pool,
                    owner_id,
                    "join",
                    "success",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=link,
                    duration_ms=dur_ms,
                )
                try:
                    from services.flood_engine import record_success

                    await record_success(acc["id"], "join")
                except Exception:
                    log_exc_swallow(
                        log,
                        f"Сбой записи успешного join в flood_engine для аккаунта {acc['id']}",
                    )
                _infra_mem.record_account_op(
                    acc["id"], "join", success=True, duration_s=dur_ms / 1000
                )
            except Exception as e:
                fail_count += 1
                err_str = str(e)[:200]
                flood_wait = extract_flood_wait(e, err_str)
                if link not in failed_links:
                    failed_links.append(link)
                _infra_mem.record_account_op(
                    acc["id"], "join", success=False, error=err_str[:100]
                )
                if _is_dead_session_error(err_str):
                    try:
                        await pool.execute(
                            """UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired',
                                   status_reason=$2 WHERE id=$1 AND is_active=TRUE""",
                            acc["id"], f"Dead session (bulk_join): {err_str[:180]}",
                        )
                        log.warning("op_worker bulk_join: deactivated dead session acc_id=%s", acc["id"])
                    except Exception as e:
                        log.warning("bulk_join: dead session deactivate failed for acc %s: %s", acc["id"], e)
                elif flood_wait:
                    try:
                        from services.flood_engine import record_flood

                        await record_flood(pool, acc["id"], flood_wait, "join", op_id)
                    except Exception:
                        log_exc_swallow(
                            log,
                            f"Сбой записи flood в flood_engine для аккаунта {acc['id']}",
                        )
                else:
                    log.warning(
                        "op_worker bulk_join: link=%s acc=%s error: %s",
                        link,
                        acc_dict.get("phone"),
                        err_str,
                    )
                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id,
                    step,
                    link,
                    err_str,
                )
                await _audit(
                    pool,
                    owner_id,
                    "join",
                    "flood_wait" if flood_wait else "error",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=link,
                    error_msg=err_str,
                    flood_wait_s=flood_wait or None,
                )
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
            # Apply pacing based on delay_mode from params
            chaos = session_simulator.chaos_factor()
            # Ночной режим — по ЛОКАЛЬНОМУ времени аккаунта (гео прокси), а не сервера.
            tod = geo_tempo.local_factor(acc.get("geo_country"), account_id=acc["id"])
            if delay_mode == "fast":
                pause = gaussian_delay(67.5 * chaos, minimum=25.0, maximum=120.0)
            elif delay_mode == "normal":
                pause = gaussian_delay(45.0 * chaos * tod, minimum=20.0, maximum=100.0)
            elif delay_mode == "slow":
                pause = gaussian_delay(90.0 * chaos * tod, minimum=35.0, maximum=180.0)
            else:  # smart — adaptive anti-flood
                if i % 5 == 4:
                    pause = gaussian_delay(270.0 * chaos, minimum=120.0, maximum=420.0)
                else:
                    pause = gaussian_delay(82.5 * chaos, minimum=30.0, maximum=150.0)
                pause *= tod
            pause = max(pause, recommended_delay(acc["id"], "join"))
            # Базовый темп вступлений — под глобальным губернатором (давление флота).
            pause = await _governed_delay(pool, owner_id, pause)
            if flood_wait:
                pause = max(
                    pause,
                    gaussian_delay(
                        float(flood_wait) + 20.0,
                        minimum=float(flood_wait) + 5.0,
                        maximum=float(flood_wait) + 45.0,
                    ),
                )
            await asyncio.sleep(pause)

        # Пауза при смене аккаунта — защита от account-hopping detection
        if acc_idx < len(accounts) - 1:
            await session_simulator.between_accounts_pause(acc_idx)

    parts = [f"Вступлено: {ok_count}", f"ошибок: {fail_count}"]
    if skipped_by_limit:
        parts.append(f"пропущено (лимит): {skipped_by_limit}")
    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "skipped_accounts": skipped_by_limit,
        "failed_links": failed_links[:50],
        "summary": ", ".join(parts),
    }


async def _exec_find_contact(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Найти потерянный контакт по имени профиля + началу @username.

    Два прохода (см. services/contact_finder): нативный поиск (безопасно) + перебор
    @prefix+цифры через ResolveUsername с паузами/ротацией аккаунтов на FloodWait и
    ранней остановкой при совпадении имени. Резолв username — лёгкий вызов (легче
    инвайта), но всё равно паузим и не молотим вслепую (слой массовых действий).
    """
    from services import account_manager, contact_finder
    from services import global_search_engine as gse

    name = str(params.get("name") or "").strip()
    prefix = str(params.get("username_prefix") or "").lstrip("@").strip()
    digits = int(params.get("digits") or 0)
    offset = max(0, int(params.get("offset") or 0))
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not prefix or not contact_finder.valid_username(prefix + "0" * max(1, digits)):
        return {"status": "failed", "summary": "⚠️ Некорректное начало @username."}
    if digits < 1 or digits > 4:
        return {"status": "failed", "summary": "⚠️ Число цифр должно быть 1–4."}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=account_ids or None, action_type="default",
    )
    accounts = await _claim_available_accounts(op_id, accounts)
    used_acc_ids = [int(a["id"]) for a in accounts]
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет свободных аккаунтов для поиска."}

    async def _log(target: str, status: str, message: str) -> None:
        await _safe_execute(
            pool,
            "INSERT INTO operation_log(op_id, step_num, target, status, message)"
            " VALUES($1,0,$2,$3,$4)",
            op_id, target[:120], status, message[:400],
            log_ctx=f"[find_contact op={op_id}]",
        )

    found_all: dict[str, dict] = {}   # username(lower) → rec, дедуп между проходами
    matches: list[dict] = []

    def _record(rec: dict) -> bool:
        """Вернёт True, если это совпадение по имени (для раннего стопа)."""
        key = (rec.get("username") or "").lower()
        if key and key not in found_all:
            found_all[key] = rec
        if contact_finder.name_matches(rec.get("name"), name):
            if not any((m.get("username") or "").lower() == key for m in matches):
                matches.append(rec)
            return True
        return False

    try:
        # ── Проход 1: нативный поиск (safe, мгновенно) ───────────────────────────
        acc0 = dict(accounts[0])
        for q in filter(None, [prefix, name]):
            try:
                res = await asyncio.wait_for(
                    gse.search_public(acc0["session_str"], q, 30, _acc=acc0), timeout=40
                )
            except Exception as e:
                log.warning("find_contact op=%d нативный поиск q=%r: %s", op_id, q, e)
                continue
            for r in (res.get("results") or []):
                if r.get("type") != "user":
                    continue
                un = (r.get("username") or "")
                rec = {"username": un, "user_id": r.get("id"),
                       "name": r.get("title") or "", "premium": False}
                # Релевантно: username начинается с префикса ИЛИ имя совпало.
                if un.lower().startswith(prefix.lower()) or contact_finder.name_matches(rec["name"], name):
                    is_match = _record(rec)
                    await _log(
                        "@" + un if un else str(rec["user_id"]),
                        "ok" if is_match else "info",
                        ("🎯 совпадение имени: " if is_match else "🌐 нативный поиск: ")
                        + f"{rec['name']} (@{un or '—'})",
                    )

        if matches:
            # Нативный проход уже нашёл — перебор не нужен.
            return _find_contact_summary(name, prefix, matches, found_all,
                                         checked=0, total_planned=0, native_only=True)

        # ── Проход 2: перебор @prefix+цифры ──────────────────────────────────────
        all_cands = list(contact_finder.iter_candidates(prefix, digits, cap=10 ** digits))
        cands = all_cands[offset: offset + contact_finder.MAX_CANDIDATES_PER_RUN]
        total_remaining = len(all_cands) - offset
        await _safe_execute(
            pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
            len(cands), op_id,
        )

        # Клиент на текущий аккаунт; ротация на следующий при FloodWait.
        state = {"idx": 0, "client": None}

        async def _ensure_client():
            if state["client"] is None:
                acc = dict(accounts[state["idx"]])
                state["client"] = await account_manager.connect_client(
                    acc["session_str"], acc, "resolve")
            return state["client"]

        async def _drop_client():
            if state["client"] is not None:
                try:
                    await state["client"].disconnect()
                except Exception:
                    pass
                state["client"] = None

        async def _resolve(uname: str) -> dict:
            try:
                client = await _ensure_client()
            except Exception as e:
                # Аккаунт не подключился — сигналим как FloodWait-подобный откат,
                # чтобы hunt ротировал на следующий аккаунт, а не считал проверенным.
                await _drop_client()
                return {"username": uname, "exists": False, "user_id": None,
                        "name": "", "premium": False, "flood_wait": 1,
                        "error": f"connect:{str(e)[:60]}"}
            return await contact_finder.resolve_on_client(client, uname)

        _progress = {"n": 0}

        async def _on_result(rec: dict) -> None:
            _record(rec)
            await _log("@" + (rec.get("username") or ""), "info",
                       f"👤 существует: {rec.get('name') or '—'} (@{rec.get('username') or '—'})")

        async def _backoff(sec: int) -> None:
            # FloodWait/сбой на текущем аккаунте → отключаем и ротируем на следующий.
            await _drop_client()
            state["idx"] = (state["idx"] + 1) % len(accounts)
            await asyncio.sleep(min(int(sec) or 1, 8))

        async def _cancelled() -> bool:
            return await _is_cancelled(pool, op_id)

        # Пейсинг: перебор username безопаснее инвайта, но не «в лоб» — 1.2с между
        # резолвами держит нас далеко от лимитов; матч останавливает перебор.
        result = await contact_finder.hunt(
            candidates=cands, target_name=name, resolve=_resolve,
            pace_s=1.2, flood_backoff_cb=_backoff, is_cancelled=_cancelled,
            on_result=_on_result, stop_on_match=True,
        )
        await _drop_client()

        checked = result["checked"]
        await _safe_execute(
            pool, "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
            min(checked, len(cands)), op_id,
        )
        return _find_contact_summary(
            name, prefix, matches, found_all,
            checked=checked, total_planned=total_remaining,
            next_offset=offset + len(cands) if (len(cands) < total_remaining and not matches) else None,
            cancelled=result.get("cancelled", False),
        )
    finally:
        await release_accounts(used_acc_ids)


def _find_contact_summary(name, prefix, matches, found_all, *, checked, total_planned,
                          next_offset=None, native_only=False, cancelled=False) -> dict:
    """Собрать честный человекочитаемый итог поиска контакта."""
    def _line(rec):
        un = rec.get("username") or "—"
        link = f"https://t.me/{un}" if un != "—" else ""
        nm = rec.get("name") or "—"
        return f"• {nm} — @{un}" + (f" · {link}" if link else "")

    if matches:
        head = f"🎯 Нашёл {len(matches)} совпадение(й) по имени «{name}»:"
        body = "\n".join(_line(m) for m in matches[:20])
        tail = "" if native_only else f"\nПроверено вариантов: {checked}."
        return {"status": "done", "found": len(matches),
                "summary": f"{head}\n{body}{tail}"}

    parts = [f"😕 Точного совпадения имени «{name}» не найдено."]
    if found_all:
        parts.append(f"Существующих @{prefix}*-юзернеймов найдено: {len(found_all)} "
                     "(имя не совпало) — см. лог операции.")
    if cancelled:
        parts.append("Операция отменена.")
    if next_offset is not None:
        parts.append(f"Проверено {checked} из {total_planned}. Остаток НЕ потерян — "
                     f"запустите поиск ещё раз для продолжения (offset={next_offset}).")
    elif not native_only:
        parts.append(f"Проверено вариантов: {checked}.")
    return {"status": "done", "found": 0, "summary": "\n".join(parts)}


async def _exec_bulk_leave(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выйти из списка каналов/групп несколькими аккаунтами."""
    from services import account_manager
    from services.flood_engine import gaussian_delay, recommended_delay

    channels = params.get("channels", [])
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not channels:
        return {
            "status": "done",
            "ok": 0,
            "failed": 0,
            "skipped_accounts": 0,
            "failed_channels": [],
            "summary": "Список каналов пуст — нечего выполнять.",
        }

    accounts_raw = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=account_ids or None,
        action_type="leave",
        respect_daily_budget=True,  # долговечность: щадим выжатые аккаунты
    )
    accounts = await _claim_available_accounts(op_id, accounts_raw, owner_id)
    used_acc_ids = [int(a["id"]) for a in accounts]

    if not accounts:
        return {
            "status": "requeue",
            "summary": "⏳ Bulk Leave: флот занят другими операциями — операция в очереди",
        }

    ok_count = 0
    fail_count = 0
    step = 0
    skipped_by_limit = 0
    failed_channels: list[str] = []
    delay_mode = params.get("delay_mode", "smart")
    proxy_mode = params.get("proxy_mode", "bound")
    _LEAVE_DAY_LIMITS = {"fast": 25, "normal": 20, "slow": 10, "smart": 15}
    day_limit = _LEAVE_DAY_LIMITS.get(delay_mode, 15)

    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
        len(channels) * len(accounts), op_id,
    )

    for acc_idx, acc in enumerate(accounts):
        if proxy_mode == "relay":
            acc_dict = {**dict(acc), "proxy_url": None, "enforce_proxy": False}
        else:
            acc_dict = dict(acc)
        try:
            leaves_today = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_audit "
                "WHERE account_id=$1 AND action='leave' AND result='success' "
                "AND occurred_at > NOW() - INTERVAL '24 hours'",
                acc["id"],
            )
        except Exception:
            log.debug('leaves_today query failed, defaulting to 0')
            leaves_today = 0
        if (leaves_today or 0) >= day_limit:
            log.info(
                "bulk_leave: аккаунт %s достиг дневного лимита leave (%d), пропуск",
                acc_dict.get("phone"),
                day_limit,
            )
            skipped_by_limit += 1
            continue
        # Риск-пульс (Волна S/1B, fail-open): карантинный аккаунт не трогаем.
        if await _infra_mem.is_account_quarantined(pool, acc["id"]):
            log.info(
                "bulk_leave: аккаунт %s в карантине (недавнее ограничение), пропуск",
                acc_dict.get("phone"),
            )
            skipped_by_limit += 1
            continue
        for i, channel in enumerate(channels):
            if await _is_cancelled(pool, op_id):
                await release_accounts(used_acc_ids)
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "failed": fail_count,
                    "skipped_accounts": skipped_by_limit,
                    "failed_channels": failed_channels[:50],
                    "summary": f"Отменено. Вышли: {ok_count}, ошибок: {fail_count}",
                }
            step += 1
            t0 = time.monotonic()
            flood_wait = 0
            try:
                res = await account_manager.leave_channel(
                    acc["session_str"], channel, _acc=acc_dict
                )
                # Proxy error → do NOT retry with different IP (causes auth key collision).
                if res.get("proxy_error") and proxy_mode == "bound":
                    _logmsg, _usermsg = _proxy_skip_reason(acc)
                    log.warning("bulk_leave: acc=%s %s", acc_dict.get("phone", "?"), _logmsg)
                    await _safe_execute(
                        pool,
                        "INSERT INTO operation_log(op_id, step_num, target, status, message)"
                        " VALUES($1,$2,$3,'error',$4)",
                        op_id, step, str(channel), _usermsg,
                        log_ctx=f"[bulk_leave_proxy_skip op={op_id}]",
                    )
                    break  # stop all remaining channels for this account
                if not res.get("ok"):
                    raise Exception(res.get("error") or f"leave_channel failed for {channel}")
                ok_count += 1
                dur_ms = int((time.monotonic() - t0) * 1000)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok','left')",
                    op_id,
                    step,
                    str(channel),
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
                await _audit(
                    pool,
                    owner_id,
                    "leave",
                    "success",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=str(channel),
                    duration_ms=dur_ms,
                )
                try:
                    from services.flood_engine import record_success

                    await record_success(acc["id"], "leave")
                except Exception:
                    log_exc_swallow(
                        log,
                        f"Сбой записи успешного leave в flood_engine для аккаунта {acc['id']}",
                    )
                _infra_mem.record_account_op(
                    acc["id"], "leave", success=True, duration_s=dur_ms / 1000
                )
            except Exception as e:
                fail_count += 1
                err_str = str(e)[:200]
                flood_wait = extract_flood_wait(e, err_str)
                ch_str = str(channel)
                if ch_str not in failed_channels:
                    failed_channels.append(ch_str)
                _infra_mem.record_account_op(
                    acc["id"], "leave", success=False, error=err_str[:100]
                )
                if flood_wait:
                    try:
                        from services.flood_engine import record_flood

                        await record_flood(pool, acc["id"], flood_wait, "leave", op_id)
                    except Exception:
                        log_exc_swallow(
                            log,
                            f"Сбой записи flood в flood_engine для аккаунта {acc['id']}",
                        )
                else:
                    log.warning(
                        "op_worker bulk_leave: channel=%s acc=%s error: %s",
                        channel,
                        acc_dict.get("phone"),
                        err_str,
                    )
                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id,
                    step,
                    str(channel),
                    err_str,
                )
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
                await _audit(
                    pool,
                    owner_id,
                    "leave",
                    "flood_wait" if flood_wait else "error",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=str(channel),
                    error_msg=err_str,
                    flood_wait_s=flood_wait or None,
                )
            # Apply pacing based on delay_mode from params
            chaos = session_simulator.chaos_factor()
            # Ночной режим — по ЛОКАЛЬНОМУ времени аккаунта (гео прокси), а не сервера.
            tod = geo_tempo.local_factor(acc.get("geo_country"), account_id=acc["id"])
            if delay_mode == "fast":
                pause = gaussian_delay(67.5 * chaos, minimum=25.0, maximum=120.0)
            elif delay_mode == "normal":
                pause = gaussian_delay(52.5 * chaos * tod, minimum=20.0, maximum=120.0)
            elif delay_mode == "slow":
                pause = gaussian_delay(90.0 * chaos * tod, minimum=35.0, maximum=180.0)
            else:  # smart — адаптивный с cooldown каждые 5
                if i % 5 == 4:
                    pause = gaussian_delay(180.0 * chaos, minimum=90.0, maximum=300.0)
                else:
                    pause = gaussian_delay(67.5 * chaos, minimum=25.0, maximum=120.0)
                pause *= tod
            pause = max(pause, recommended_delay(acc["id"], "leave"))
            if flood_wait:
                pause = max(
                    pause,
                    gaussian_delay(
                        float(flood_wait) + 20.0,
                        minimum=float(flood_wait) + 5.0,
                        maximum=float(flood_wait) + 45.0,
                    ),
                )
            await asyncio.sleep(pause)

        # Пауза при смене аккаунта — защита от account-hopping detection
        if acc_idx < len(accounts) - 1:
            await session_simulator.between_accounts_pause(acc_idx)

    await release_accounts(used_acc_ids)
    parts = [f"Вышли: {ok_count}", f"ошибок: {fail_count}"]
    if skipped_by_limit:
        parts.append(f"пропущено (лимит): {skipped_by_limit}")
    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "skipped_accounts": skipped_by_limit,
        "failed_channels": failed_channels[:50],
        "summary": ", ".join(parts),
    }


async def _revive_abandoned_gp_targets(pool: asyncpg.Pool, plan_id: int, what: str) -> None:
    """Расклинить цели плана Global Presence, брошенные прошлым прогоном.

    Цель берётся атомарным переводом pending → running, и снимает этот статус
    только тот же прогон. Если воркер умер между занятием и исходом — деплой,
    падение, потолок прогона — цель остаётся в 'running' НАВСЕГДА: исполнитель
    при повторе выбирает `status='pending'`, и брошенная цель больше не
    рассматривается никогда. План молча не достраивается: владелец видит «22 из
    30» и никакой ошибки, потому что ошибки и не было.

    Разбор по `result_asset_id`, а не по одному возрасту, и это принципиально:

      • пусто → ресурс создать не успели, работа не начиналась. Честно
        возвращаем в очередь, повтор сделает её с нуля.
      • заполнено → канал/бот в Telegram УЖЕ СОЗДАН. Вернуть такую цель в
        очередь значит создать второй такой же — для аккаунта это лишнее
        дорогое действие и лишний повод для ограничений. Закрываем цель как
        выполненную и пишем в error_message, что довеска (имя, аватар, запись в
        каталог) могло не примениться: ресурс есть, и его подберёт сканирование
        собственных ресурсов.

    Не бросает: это расклинивание, а не критический путь.
    """
    try:
        requeued = await _safe_execute(
            pool,
            "UPDATE global_presence_targets SET status='pending' "
            " WHERE plan_id=$1 AND status='running' AND result_asset_id IS NULL",
            plan_id, log_ctx=f"[gp_revive_pending plan={plan_id}]",
        )
        closed = await _safe_execute(
            pool,
            "UPDATE global_presence_targets SET status='done', "
            "       error_message=COALESCE(error_message, $2) "
            " WHERE plan_id=$1 AND status='running' AND result_asset_id IS NOT NULL",
            plan_id,
            "Ресурс создан, но прогон оборвался — оформление могло не примениться. "
            "Проверьте сканированием собственных ресурсов.",
            log_ctx=f"[gp_revive_done plan={plan_id}]",
        )
    except Exception:
        log_exc_swallow(log, f"gp: расклинивание целей плана {plan_id}")
        return
    def _n(res):
        try:
            return int(str(res).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0
    if _n(requeued) or _n(closed):
        log.warning(
            "gp %s: план %d — расклинено брошенных целей: возвращено в очередь %d, "
            "закрыто как созданные %d",
            what, plan_id, _n(requeued), _n(closed),
        )


async def _exec_global_presence_channel(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать каналы или группы для всех ожидающих целей плана global_presence."""
    from services import account_manager

    plan_id = params.get("plan_id")
    if not plan_id:
        return {"status": "failed", "reason": "Не указан plan_id"}

    plan = await _safe_fetchrow(
            pool,
        "SELECT asset_type FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )
    if not plan:
        return {"status": "failed", "reason": "План не найден"}

    asset_type = plan.get("asset_type", "channel")
    # Тип плана — лишь запасной вариант. Решает тип КАЖДОЙ цели: проект-генератор
    # кладёт в один план каналы (новости, работа, афиша) и группы (чат, барахолка)
    # вперемешку, и общий флаг превратил бы половину структуры не в тот тип актива.
    plan_is_group = asset_type == "group"

    await _safe_execute(
            pool,
        "UPDATE global_presence_plans SET status='running', updated_at=now() WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )

    # Цели, брошенные прошлым прогоном, застревают в 'running' и в выборку ниже
    # уже не попадают — план молча не достраивается. Расклиниваем их до выборки.
    await _revive_abandoned_gp_targets(pool, plan_id, "channel")

    targets = await _safe_fetch(
            pool,
        "SELECT * FROM global_presence_targets WHERE plan_id=$1 AND status='pending' ORDER BY id",
        plan_id,
    )
    if not targets:
        await _safe_execute(
                pool,
            "UPDATE global_presence_plans SET status='done', updated_at=now() WHERE id=$1",
            plan_id,
        )
        return {
            "status": "done",
            "created": 0,
            "failed": 0,
            "summary": "Нет ожидающих целей",
        }

    acc_ids = list(
        {t["selected_account_id"] for t in targets if t["selected_account_id"]}
    )
    if not acc_ids:
        return {"status": "failed", "reason": "Нет аккаунтов для выполнения"}

    # Отказной захват всего пула: дальше аккаунты выбираются из него по ходу
    # (в т.ч. запасной при карантине), поэтому захватываем пул целиком.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts_rows])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts_rows) - len(claimed_ids)
    accounts_rows = [a for a in accounts_rows if int(a["id"]) in set(claimed_ids)]

    try:

        accounts_rows = await resource_selector.select_all_active(
            pool, owner_id, include_ids=acc_ids, respect_cooldown=False
        )
        acc_by_id = {a["id"]: dict(a) for a in accounts_rows}

        created_count = 0
        failed_count = 0
        total = len(targets)
        _gp_eco_id: int | None = None  # lazily loaded from plan
        await _safe_execute(
                pool,
            "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
        )

        for i, target in enumerate(targets):
            if await _is_cancelled(pool, op_id):
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1",
                    plan_id,
                )
                return {
                    "status": "cancelled",
                    "created": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }

            is_group = (target.get("asset_type") or asset_type) == "group"

            acc_id = target["selected_account_id"]
            acc = acc_by_id.get(acc_id)

            if not acc:
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                    "Аккаунт недоступен",
                    target["id"],
                )
                failed_count += 1
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            # ── Единый гейт здоровья аккаунта перед постом ──
            # trust_score — лишь одна ось; is_account_quarantined сводит весь риск-пульс
            # (restriction_events + acc_status + flood + trust + health). Без него
            # постили бы с зафлуженного/ограниченного аккаунта → риск бана (класс #7/#2).
            # fail-open: при ошибке проверки аккаунт НЕ блокируется.
            trust_score = acc.get("trust_score") or 0.5
            _quarantined = await _infra_mem.is_account_quarantined(pool, acc["id"])
            if trust_score < 0.3 or _quarantined:
                log.warning(
                    "op_worker gp_%s: skipping account %s (trust=%.2f, quarantined=%s)",
                    "group" if is_group else "channel",
                    acc["phone"],
                    trust_score,
                    _quarantined,
                )
                # Альтернатива: приемлемый trust И не под карантином.
                alt_acc = None
                for a in accounts_rows:
                    if a["id"] == acc_id or (a.get("trust_score") or 0.5) < 0.5:
                        continue
                    if await _infra_mem.is_account_quarantined(pool, a["id"]):
                        continue
                    alt_acc = dict(a)
                    log.info(
                        "op_worker gp: switching to account %s with trust=%.2f",
                        a["phone"],
                        a.get("trust_score"),
                    )
                    break

                if not alt_acc:
                    _reason = (
                        "Аккаунт под карантином (риск-пульс), запасных нет"
                        if _quarantined
                        else f"Все аккаунты имеют низкий trust_score (мин: {trust_score:.2f})"
                    )
                    await _safe_execute(
                            pool,
                        "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                        _reason,
                        target["id"],
                    )
                    failed_count += 1
                    await _safe_execute(
                            pool,
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                        op_id,
                    )
                    continue

                acc = alt_acc

            # Atomic claim: only proceed if target is still 'pending' to prevent duplicate processing
            claimed = await _safe_execute(
                    pool,
                "UPDATE global_presence_targets SET status='running' WHERE id=$1 AND status='pending'",
                target["id"],
            )
            if claimed == "UPDATE 0":
                log.info(
                    "op_worker gp: target %d already claimed by another worker, skipping",
                    target["id"],
                )
                continue

            title = (
                target["planned_name"] or f"{'Group' if is_group else 'Channel'} {i + 1}"
            )

            # Описание: пустое = немедленный spam-сигнал для Telegram. Приоритет у
            # сгенерированного (planned_about) — оно уникально для объекта и уже
            # показано пользователю в предпросмотре. Запасной вариант — прежняя
            # общая строка: она одинакова на всю сеть, поэтому только как fallback
            # для планов, собранных до генератора.
            _geo_label = (target.get("city") or target.get("country") or "").strip()
            _about = (target.get("planned_about") or "").strip()
            if not _about:
                if is_group:
                    _about = f"Группа для общения и обмена информацией.{(' ' + _geo_label) if _geo_label else ''}"
                else:
                    _about = f"Актуальные новости и обновления.{(' ' + _geo_label) if _geo_label else ''}"

            # ── Умная задержка перед созданием ──
            await session_simulator.typing_delay(title)  # 0.5-2с для натуральности

            t0_gp = time.monotonic()
            result = await account_manager.create_channel(
                acc["session_str"], title, about=_about, megagroup=is_group, _acc=acc
            )

            if result.get("error") and result.get("flood_wait"):
                raw_flood = int(result["flood_wait"])
                if raw_flood > 600:
                    # Flood wait too long to block the batch — skip this target and continue
                    log.warning(
                        "op_worker gp_%s: flood wait %ds too long for target %d — skipping",
                        "group" if is_group else "channel",
                        raw_flood,
                        target["id"],
                    )
                else:
                    wait_time = raw_flood + 15
                    log.info(
                        "op_worker gp_%s: flood wait %ds for target %d",
                        "group" if is_group else "channel",
                        wait_time,
                        target["id"],
                    )
                    await asyncio.sleep(wait_time)
                    result = await account_manager.create_channel(
                        acc["session_str"],
                        title,
                        about=_about,
                        megagroup=is_group,
                        _acc=acc,
                    )

            if result.get("error"):
                err_str = str(result["error"])
                # Немедленно деактивировать аккаунт при AUTH_KEY/SESSION ошибке
                if "AUTH_KEY" in err_str or "SESSION_REVOKED" in err_str:
                    try:
                        await pool.execute(
                            """UPDATE tg_accounts
                               SET is_active    = FALSE,
                                   acc_status   = 'session_expired',
                                   status_reason = $2
                               WHERE id = $1 AND is_active = TRUE""",
                            acc["id"],
                            f"AUTH_KEY/SESSION dead (gp_channel): {err_str[:200]}",
                        )
                        log.warning(
                            "op_worker gp_channel: deactivated dead session account_id=%s",
                            acc["id"],
                        )
                    except Exception as _dbe:
                        log.warning("op_worker gp_channel: deactivate failed: %s", _dbe)
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                    err_str[:500],
                    target["id"],
                )
                failed_count += 1
                _infra_mem.record_account_op(
                    acc["id"],
                    "global_presence_channel",
                    success=False,
                    error=err_str[:100],
                )
                await _audit(
                    pool,
                    owner_id,
                    "gp_create_group" if is_group else "gp_create_channel",
                    "flood_wait" if result.get("flood_wait") else "error",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=title[:100],
                    error_msg=err_str[:200],
                    flood_wait_s=int(result["flood_wait"])
                    if result.get("flood_wait")
                    else None,
                )
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                await asyncio.sleep(
                    random.uniform(10, 25) * session_simulator.chaos_factor()
                )
                continue

            channel_id = result.get("channel_id")
            channel_access_hash = result.get("access_hash", 0)
            # Отмечаем созданный ресурс СРАЗУ. Дальше идёт пауза 90-180с перед
            # установкой имени (анти-детект) и оформление — окно в минуты, и
            # смерть процесса в нём оставляла цель в 'running' без следа. По
            # этой отметке расклинивание отличает «канал уже создан» от «работа
            # не начиналась» и не создаёт второй канал.
            if channel_id:
                await _safe_execute(
                    pool,
                    "UPDATE global_presence_targets SET result_asset_id=$1 WHERE id=$2",
                    channel_id, target["id"],
                    log_ctx=f"[gp_mark_created target={target['id']}]",
                )

            username_error = None
            planned_username = target.get("planned_username")
            # Какой username РЕАЛЬНО встал. Раньше в каталог писался planned_*
            # независимо от исхода: при занятом имени канал уходил в managed_channels
            # с username, которого у него нет, — ссылки из отчёта вели в никуда.
            applied_username: str | None = None
            if planned_username and channel_id:
                # Пауза 90-180с перед установкой username — Telegram детектирует мгновенное
                # присвоение username как автоматизацию и применяет geo-ban / shadow-ban
                pause = random.uniform(90, 180) * session_simulator.chaos_factor()
                log.info(
                    "op_worker gp_channel: waiting %.0fs before assigning username '%s'",
                    pause,
                    planned_username,
                )
                await asyncio.sleep(pause)
                err = await account_manager.set_channel_username(
                    acc["session_str"], channel_id, planned_username, _acc=acc
                )
                if not err:
                    applied_username = planned_username
                if err:
                    log.info(
                        "op_worker gp_channel: username '%s' failed (%s), trying variants",
                        planned_username,
                        err[:80],
                    )
                    if "flood" in err.lower() or "FloodWait" in err:
                        import re as _re

                        m = _re.search(r"(\d+)", err)
                        flood_wait = int(m.group(1)) + 5 if m else 60
                        if flood_wait > _FLOOD_INLINE_MAX_S:
                            # Пауза без предела здесь держала слот и аккаунты
                            # часами ради ОДНОГО имени: канал к этому моменту
                            # уже создан, и без имени он остаётся рабочим.
                            # Ветка перебора вариантов ниже уже обрывается на
                            # своём пределе (600с) — приводим и этот сон к тому
                            # же принципу вместо бесконечного ожидания.
                            log.warning(
                                "op_worker gp_channel: FloodWait %ds превышает предел %ds — "
                                "имя не ставим, канал остаётся без username",
                                flood_wait, _FLOOD_INLINE_MAX_S,
                            )
                            # username_error проставляется ниже по итогу перебора
                            # вариантов — здесь его трогать нельзя, затрётся.
                            flood_wait = 0
                        else:
                            log.info(
                                "op_worker gp_channel: FloodWait %ds, sleeping...", flood_wait
                            )
                            await asyncio.sleep(flood_wait)
                    from services.username_engine import generate_username_variants

                    geo = {
                        "country_code": target.get("country_code", ""),
                        "city": target.get("city", ""),
                        "city_slug": target.get("city_slug", ""),
                    }
                    # ── Расширенная генерация вариантов username ──
                    from services.username_engine import slugify

                    variants = generate_username_variants(planned_username, geo)

                    # Добавляем город + случайное число
                    city_slug = slugify(geo.get("city", ""))[:10] if geo else ""
                    cc = slugify(geo.get("country_code", ""))[:3] if geo else ""
                    for num in [10, 15, 20, 25, 30, 35, 40, 45, 50]:
                        if city_slug:
                            variants.append(f"{city_slug}_{num}")
                        if cc and city_slug:
                            variants.append(f"{cc}_{city_slug}{num}")
                    # Случайные числовые суффиксы
                    import random as _random

                    for _ in range(12):
                        variants.append(f"{planned_username}_{_random.randint(100, 999)}")

                    # Дедупликация
                    seen = {planned_username}
                    final_variants = []
                    for v in variants:
                        if v not in seen and len(v) <= 32:
                            seen.add(v)
                            final_variants.append(v)

                    success_variant = None
                    for variant in final_variants[:8]:
                        if variant == planned_username:
                            continue  # уже пробовали
                        await asyncio.sleep(random.uniform(5, 12))
                        err2 = await account_manager.set_channel_username(
                            acc["session_str"], channel_id, variant, _acc=acc
                        )
                        if not err2:
                            log.info(
                                "op_worker gp_channel: username variant '%s' accepted",
                                variant,
                            )
                            success_variant = variant
                            applied_username = variant
                            err = None
                            break
                        log.info(
                            "op_worker gp_channel: variant '%s' also failed: %s",
                            variant,
                            err2[:60],
                        )
                        # Flood wait handling — cap at 600s; longer waits abort variant loop
                        if "FloodWait" in str(err2):
                            m2 = _re.search(r"(\d+)", str(err2))
                            fw = int(m2.group(1)) + 5 if m2 else 30
                            if fw > 600:
                                log.warning(
                                    "op_worker gp_channel: FloodWait %ds for username exceeds cap, aborting variants",
                                    fw,
                                )
                                break
                            await asyncio.sleep(fw)
                    username_error = err if not success_variant else None

            # ── Аватар: канал без фото Telegram трактует как заготовку — хуже
            # ранжируется и чаще ловит ограничения. Картинка детерминирована по
            # avatar_seed, поэтому ставится ровно та, что была в предпросмотре.
            avatar_ok = False
            _avatar_seed = target.get("avatar_seed")
            if _avatar_seed is not None and channel_id:
                try:
                    from services import avatar_factory

                    _photo = await asyncio.to_thread(
                        avatar_factory.generate_avatar,
                        int(_avatar_seed),
                        title,
                        style=target.get("avatar_style") or None,
                    )
                    await asyncio.sleep(random.uniform(4, 11) * session_simulator.chaos_factor())
                    _ph_err = await account_manager.set_channel_photo(
                        acc["session_str"],
                        channel_id,
                        _photo,
                        access_hash=int(channel_access_hash or 0),
                        _acc=acc,
                    )
                    avatar_ok = not _ph_err
                    if _ph_err:
                        log.info(
                            "op_worker gp: avatar not applied for target %d: %s",
                            target["id"],
                            _ph_err[:120],
                        )
                except Exception as _av_err:
                    # Аватар — оформление, а не суть объекта: его сбой не должен
                    # ронять уже созданный канал в 'failed'.
                    log.warning("op_worker gp: avatar generation failed: %s", _av_err)

            # ── Атомарная запись: обновить targets + вставить в managed_channels одной транзакцией.
            # Если Telethon создал канал, но DB-запись падает, канал станет «призраком» без записи.
            # Транзакция гарантирует: либо оба write успешны, либо оба откатываются.
            # В каталог пишется ФАКТИЧЕСКИ вставший username (planned мог быть занят
            # и заменён вариантом либо не примениться вовсе) и корректный тип: группы
            # с type='channel' не попадали ни в один групповой фильтр.
            async with pool.acquire() as _conn:
                async with _conn.transaction():
                    await _conn.execute(
                        "UPDATE global_presence_targets "
                        "SET status='done', result_asset_id=$1, final_username=$2, avatar_applied=$3 "
                        "WHERE id=$4",
                        channel_id,
                        applied_username,
                        avatar_ok,
                        target["id"],
                    )
                    await _conn.execute(
                        """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username, access_hash, type)
                           VALUES($1,$2,$3,$4,$5,$6,$7)
                           ON CONFLICT(owner_id, channel_id) DO UPDATE
                           SET title=$4, username=$5, access_hash=$6, type=$7""",
                        owner_id,
                        acc["id"],
                        channel_id,
                        title,
                        applied_username,
                        int(channel_access_hash or 0),
                        "group" if is_group else "channel",
                    )

            _infra_mem.record_account_op(
                acc["id"],
                "global_presence_channel",
                success=True,
                duration_s=time.monotonic() - t0_gp,
            )
            await _audit(
                pool,
                owner_id,
                "gp_create_group" if is_group else "gp_create_channel",
                "success",
                operation_id=op_id,
                account_id=acc["id"],
                target=title[:100],
                duration_ms=int((time.monotonic() - t0_gp) * 1000),
            )

            # Публикуем начальный пост — пустой канал немедленно попадает в shadow ban.
            # Любой пост делает канал "живым" для алгоритмов Telegram.
            try:
                _welcome_text = f"{'👥' if is_group else '📢'} {title}"
                if _geo_label:
                    _welcome_text += f"\n\n📍 {_geo_label}"
                _post_delay = random.uniform(30, 60) * session_simulator.chaos_factor()
                await asyncio.sleep(_post_delay)
                await account_manager.post_to_channel(
                    acc["session_str"],
                    channel_id,
                    _welcome_text,
                    access_hash=channel_access_hash,
                    _acc=dict(acc),
                )
                log.info(
                    "op_worker gp_channel: initial post sent to channel_id=%s", channel_id
                )
            except Exception:
                log_exc_swallow(log, f"initial post failed for channel_id={channel_id}")

            # Link to ecosystem if one exists for this owner
            try:
                ecos = await pool.fetch(
                    "SELECT id FROM ecosystems WHERE owner_id=$1 AND ecosystem_type='global_presence' AND status='active' ORDER BY created_at DESC LIMIT 1",
                    owner_id,
                )
                if ecos and channel_id:
                    from services import ecosystem_brain as _eb

                    eco_id = ecos[0]["id"]
                    obj_type = "group" if is_group else "channel"
                    await _eb.add_member(pool, eco_id, owner_id, obj_type, channel_id)
            except Exception as e:
                log_exc_swallow(log, f"gp_channel ecosystem add_member failed for ch={channel_id}: {e}")

            created_count += 1

            # Add created channel to ecosystem
            try:
                if _gp_eco_id is None:
                    _eco_row = await pool.fetchrow(
                        "SELECT ecosystem_id FROM global_presence_plans WHERE id=$1",
                        plan_id,
                    )
                    _gp_eco_id = (_eco_row["ecosystem_id"] if _eco_row else None) or 0
                if _gp_eco_id:
                    from services import ecosystem_brain as _eb

                    await _eb.add_member(pool, _gp_eco_id, owner_id, "channel", channel_id)
                    await _eb.add_member(pool, _gp_eco_id, owner_id, "account", acc["id"])
            except Exception as e:
                log_exc_swallow(log, f"gp_channel ecosystem add_member to plan {plan_id} failed: {e}")

            await _safe_execute(
                    pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                op_id,
                created_count + failed_count,
                f"{target.get('city', '?')} → {title}",
                f"channel_id={channel_id}"
                + (f" | username_err={username_error}" if username_error else ""),
            )
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            if created_count > 0 and created_count % 10 == 0:
                try:
                    await db.notify_if_enabled(
                        pool,
                        bot,
                        owner_id,
                        "op_complete",
                        f"🌍 <b>Создание каналов (план #{plan_id}):</b> {created_count + failed_count}/{total}\n"
                        f"✅ Создано: {created_count} | ❌ Ошибок: {failed_count}",
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"Сбой отправки прогресса создания каналов плана #{plan_id} владельцу {owner_id}",
                    )

            if i < total - 1:
                # ── Почитай daily rhythm и избегай ночных часов пиков ──
                tod_factor = (
                    session_simulator.time_of_day_factor()
                )  # 2-5x at night, 0.75x at peak
                chaos = session_simulator.chaos_factor()  # 0.7-1.3
                jitter = session_simulator.chaos_factor(
                    1.0, 0.1
                )  # ±10% микро-шум (sync float)

                if i % 5 == 4:
                    # Длинная пауза каждые 5 операций (имитация человеческого перерыва)
                    cooldown = random.uniform(300, 600) * chaos * tod_factor * jitter
                    log.info(
                        "op_worker gp_channel: cooldown %.0fs after %d items (tod_factor=%.2f)",
                        cooldown,
                        i + 1,
                        tod_factor,
                    )
                    await asyncio.sleep(await _governed_delay(pool, owner_id, cooldown))
                else:
                    # Короткая пауза между операциями
                    delay = random.uniform(45, 90) * chaos * tod_factor * jitter
                    await asyncio.sleep(await _governed_delay(pool, owner_id, delay))

        final_status = (
            "done" if failed_count == 0 else ("failed" if created_count == 0 else "done")
        )
        await _safe_execute(
                pool,
            "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
            final_status,
            plan_id,
        )

        _unit = "групп" if plan_is_group else "активов"
        return {
            "status": "done",
            "created": created_count,
            "failed": failed_count,
            "plan_id": plan_id,
            # «каналов» врало на планах со смешанной структурой (каналы + чаты)
            # и на групповых планах — итог обязан совпадать с тем, что создано.
            "summary": f"Создано {_unit}: {created_count}, ошибок: {failed_count}",
        }
    finally:
        await release_accounts(claimed_ids)


# Пакетные действия над объектами проекта. Значение каждого вычисляется ДЛЯ
# КАЖДОГО объекта — этим операция отличается от общего bulk_edit_channels,
# который ставит один текст на все каналы аккаунта.
GP_BULK_ACTIONS: dict[str, dict] = {
    "about": {"label": "описания", "needs_admin_rights": False},
    "avatar": {"label": "аватары", "needs_admin_rights": False},
    "both": {"label": "описания и аватары", "needs_admin_rights": False},
    "post_pin": {"label": "пост с закрепом", "needs_admin_rights": False},
    "admin": {"label": "назначение админа", "needs_admin_rights": True},
    "username": {"label": "username", "needs_admin_rights": False},
}


async def _exec_gp_bulk_apply(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Пакетные операции над уже созданными объектами проекта.

    Отличие от общего `bulk_edit_channels`: тот ставит ОДИН текст на все каналы
    аккаунта. Здесь значение вычисляется для каждого объекта отдельно — по его
    городу, роли и seed, — поэтому «обновить описания у 800 каналов» не
    превращает сеть в 800 одинаковых карточек.

    params:
      plan_id      — проект;
      action       — из GP_BULK_ACTIONS;
      about_template / post_template — шаблоны с плейсхолдерами (необязательны);
      admin_user_id — кого назначить админом (для action='admin');
      seed_shift    — сдвиг seed для перерисовки аватаров;
      only_missing  — для action='username': доназначить только тем, у кого его
                      нет (по умолчанию True — это безопаснее, чем менять всем).
    """
    from services import account_manager, avatar_factory
    from services.presence_planner import render_pattern
    from services.username_engine import UsernameAllocator

    plan_id = params.get("plan_id")
    action = params.get("action") or "both"
    about_template = (params.get("about_template") or "").strip()
    post_template = (params.get("post_template") or "").strip()
    admin_user_id = params.get("admin_user_id")
    seed_shift = int(params.get("seed_shift") or 0)
    only_missing = params.get("only_missing", True)

    if not plan_id:
        return {"status": "failed", "reason": "Не указан plan_id"}
    if action not in GP_BULK_ACTIONS:
        return {"status": "failed", "reason": f"Неизвестное действие: {action}"}
    if action == "admin" and not admin_user_id:
        return {"status": "failed", "reason": "Не указан пользователь для назначения админом"}

    plan = await _safe_fetchrow(
        pool,
        "SELECT avatar_style, username_pool FROM global_presence_plans "
        " WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )
    if not plan:
        return {"status": "failed", "reason": "План не найден"}
    avatar_style = plan.get("avatar_style") or None

    # Только реально созданные объекты: у остальных нечего править.
    # Для username — при only_missing берём лишь те, где его так и не встало
    # (частый исход: имя было занято и все варианты исчерпались).
    extra_where = ""
    if action == "username" and only_missing:
        extra_where = " AND t.final_username IS NULL"

    targets = await _safe_fetch(
        pool,
        f"""SELECT t.*, m.access_hash
             FROM global_presence_targets t
             LEFT JOIN managed_channels m
               ON m.owner_id = $2 AND m.channel_id = t.result_asset_id
            WHERE t.plan_id = $1
              AND t.status = 'done'
              AND t.result_asset_id IS NOT NULL
              AND t.asset_type <> 'bot'{extra_where}
            ORDER BY t.id""",
        plan_id,
        owner_id,
    )
    if not targets:
        return {
            "status": "done",
            "ok": 0,
            "fail": 0,
            "summary": "Нет подходящих объектов для применения",
        }

    acc_ids = list({t["selected_account_id"] for t in targets if t["selected_account_id"]})
    accounts_rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=acc_ids, respect_cooldown=False
    )

    # Отказной захват всего пула: дальше аккаунты выбираются из него по ходу
    # (в т.ч. запасной при карантине), поэтому захватываем пул целиком.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts_rows])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts_rows) - len(claimed_ids)
    accounts_rows = [a for a in accounts_rows if int(a["id"]) in set(claimed_ids)]

    try:
        acc_by_id = {a["id"]: dict(a) for a in accounts_rows}

        # Аллокатор для доназначения username: он обязан видеть все уже занятые
        # имена владельца, иначе выдаст то, что у нас же и стоит.
        allocator = None
        uname_templates: list[str] = []
        if action == "username":
            from database import db as _db

            allocator = UsernameAllocator(
                taken=await _db.get_taken_usernames(pool, owner_id), seed=seed_shift or None
            )
            raw_pool = plan.get("username_pool")
            if raw_pool:
                try:
                    import json as _json

                    uname_templates = (
                        raw_pool if isinstance(raw_pool, list) else _json.loads(raw_pool)
                    )
                except Exception:
                    uname_templates = []

        await _safe_execute(
            pool, "UPDATE operation_queue SET total_items=$1, done_items=0 WHERE id=$2",
            len(targets), op_id,
        )

        counters = {"about": 0, "avatar": 0, "post": 0, "pin": 0, "admin": 0, "username": 0}
        failed = skipped = 0

        for target in targets:
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": sum(counters.values()),
                    "fail": failed,
                    "summary": f"Отменено. Применено: {sum(counters.values())}, ошибок: {failed}",
                }

            acc = acc_by_id.get(target["selected_account_id"])
            if not acc or not acc.get("session_str"):
                skipped += 1
                await _safe_execute(
                    pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            # Единый гейт здоровья: правка оформления — тоже действие от лица
            # аккаунта, и с зафлуженного/ограниченного её делать нельзя.
            if await _infra_mem.is_account_quarantined(pool, acc["id"]):
                skipped += 1
                await _safe_execute(
                    pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            channel_id = target["result_asset_id"]
            access_hash = int(target.get("access_hash") or 0)
            title = target.get("planned_name") or ""
            item_failed = False

            if action in ("about", "both"):
                if about_template:
                    # Шаблон разворачивается ДЛЯ КАЖДОГО объекта: «Новости
                    # {{CITY_GEN}}» даёт свой текст в каждом городе, а не один
                    # общий на всю сеть.
                    new_about = render_pattern(about_template, dict(target))[:255]
                else:
                    new_about = (target.get("planned_about") or "").strip()
                if new_about:
                    try:
                        ok = await account_manager.edit_channel_about(
                            acc["session_str"], channel_id, new_about, _acc=acc
                        )
                        if ok:
                            counters["about"] += 1
                            await _safe_execute(
                                pool,
                                "UPDATE global_presence_targets SET planned_about=$1 WHERE id=$2",
                                new_about,
                                target["id"],
                            )
                        else:
                            item_failed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log_exc_swallow(log, f"gp_bulk_apply about target={target['id']}: {exc}")
                        item_failed = True
                    await asyncio.sleep(random.uniform(3, 8) * session_simulator.chaos_factor())

            if action in ("avatar", "both"):
                base_seed = target.get("avatar_seed")
                if base_seed is None:
                    # У целей из планов до генератора seed'а нет — выводим его из
                    # id, чтобы перерисовка оставалась воспроизводимой.
                    base_seed = target["id"] * 2654435761 % (2**31)
                try:
                    photo = await asyncio.to_thread(
                        avatar_factory.generate_avatar,
                        int(base_seed) + seed_shift,
                        title,
                        style=target.get("avatar_style") or avatar_style,
                    )
                    ph_err = await account_manager.set_channel_photo(
                        acc["session_str"], channel_id, photo, access_hash=access_hash, _acc=acc
                    )
                    if ph_err:
                        item_failed = True
                        log.info(
                            "gp_bulk_apply: аватар не применён target=%s: %s",
                            target["id"], ph_err[:120],
                        )
                    else:
                        counters["avatar"] += 1
                        await _safe_execute(
                            pool,
                            "UPDATE global_presence_targets SET avatar_applied=TRUE WHERE id=$1",
                            target["id"],
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_exc_swallow(log, f"gp_bulk_apply avatar target={target['id']}: {exc}")
                    item_failed = True
                await asyncio.sleep(random.uniform(4, 10) * session_simulator.chaos_factor())

            if action == "post_pin":
                # Пост тоже индивидуален: одинаковый текст в 800 каналах — прямой
                # спам-сигнал, ровно то, ради чего существует генератор.
                text = render_pattern(post_template, dict(target)).strip() if post_template else ""
                if not text:
                    text = (target.get("planned_about") or title).strip()
                if not text:
                    skipped += 1
                else:
                    try:
                        res = await account_manager.post_to_channel(
                            acc["session_str"],
                            channel_id,
                            text[:4000],
                            access_hash=access_hash,
                            username=target.get("final_username") or "",
                            _acc=acc,
                        )
                        if isinstance(res, dict) and res.get("error"):
                            item_failed = True
                            log.info(
                                "gp_bulk_apply: пост не отправлен target=%s: %s",
                                target["id"], str(res["error"])[:120],
                            )
                        else:
                            counters["post"] += 1
                            await asyncio.sleep(
                                random.uniform(3, 7) * session_simulator.chaos_factor()
                            )
                            pin = await account_manager.pin_last_channel_post(
                                acc["session_str"],
                                channel_id,
                                access_hash=access_hash,
                                username=target.get("final_username") or "",
                                _acc=acc,
                            )
                            # Пост опубликован, закреп не удался — это НЕ провал
                            # элемента: контент на месте, счётчики разные.
                            if isinstance(pin, dict) and not pin.get("error"):
                                counters["pin"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log_exc_swallow(log, f"gp_bulk_apply post target={target['id']}: {exc}")
                        item_failed = True
                    await asyncio.sleep(random.uniform(20, 45) * session_simulator.chaos_factor())

            if action == "admin":
                try:
                    ok = await account_manager.promote_to_admin(
                        acc["session_str"],
                        channel_id,
                        int(admin_user_id),
                        _acc=acc,
                        access_hash=access_hash,
                        post_messages=True,
                        invite_users=True,
                        pin_messages=True,
                    )
                    if ok:
                        counters["admin"] += 1
                    else:
                        item_failed = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_exc_swallow(log, f"gp_bulk_apply admin target={target['id']}: {exc}")
                    item_failed = True
                await asyncio.sleep(random.uniform(5, 12) * session_simulator.chaos_factor())

            if action == "username":
                ctx = {
                    "city": target.get("city_slug") or "",
                    "city_slug": target.get("city_slug") or "",
                    "country": target.get("country") or "",
                    "country_code": target.get("country_code") or "",
                    "region": target.get("region") or "",
                    "role": target.get("role") or "",
                    "index": target["id"],
                }
                candidate = allocator.allocate(uname_templates, ctx) if allocator else None
                if not candidate:
                    skipped += 1
                else:
                    try:
                        err = await account_manager.set_channel_username(
                            acc["session_str"], channel_id, candidate, _acc=acc
                        )
                        if err:
                            item_failed = True
                            # Имя занято кем-то снаружи — резервируем, чтобы
                            # следующим объектам оно не предлагалось повторно.
                            allocator.reserve(candidate)
                            log.info(
                                "gp_bulk_apply: username '%s' не встал target=%s: %s",
                                candidate, target["id"], err[:120],
                            )
                        else:
                            counters["username"] += 1
                            async with pool.acquire() as _c:
                                async with _c.transaction():
                                    await _c.execute(
                                        "UPDATE global_presence_targets "
                                        "SET final_username=$1 WHERE id=$2",
                                        candidate, target["id"],
                                    )
                                    await _c.execute(
                                        "UPDATE managed_channels SET username=$1 "
                                        " WHERE owner_id=$2 AND channel_id=$3",
                                        candidate, owner_id, channel_id,
                                    )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log_exc_swallow(log, f"gp_bulk_apply username target={target['id']}: {exc}")
                        item_failed = True
                    # Присвоение username — самое чувствительное к темпу действие:
                    # Telegram трактует частые UpdateUsername как автоматизацию.
                    await asyncio.sleep(random.uniform(90, 180) * session_simulator.chaos_factor())

            if item_failed:
                failed += 1
            await _safe_execute(
                pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

        labels = {
            "about": "описаний", "avatar": "аватаров", "post": "постов",
            "pin": "закреплено", "admin": "назначений", "username": "username",
        }
        parts = [f"{labels[k]}: {v}" for k, v in counters.items() if v]
        if not parts:
            parts.append("применено: 0")
        if skipped:
            # Пропуски показываем отдельно от ошибок: «аккаунт в карантине» — это
            # не сбой применения, и смешивать их в одном счётчике нечестно.
            parts.append(f"пропущено: {skipped}")
        if failed:
            parts.append(f"ошибок: {failed}")

        return {
            "status": "done",
            "ok": sum(counters.values()),
            "fail": failed,
            "plan_id": plan_id,
            "summary": "🧰 Пакетное применение — " + ", ".join(parts),
        }
    finally:
        await release_accounts(claimed_ids)



async def _exec_global_presence_bot(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать ботов через BotFather для каждой цели плана global_presence."""
    from services import account_manager

    plan_id = params.get("plan_id")
    if not plan_id:
        return {"status": "failed", "reason": "no plan_id in params"}

    plan = await _safe_fetchrow(
            pool,
        "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )
    if not plan:
        return {"status": "failed", "reason": f"plan {plan_id} not found"}

    account_selection = plan["account_selection"] or {}
    if isinstance(account_selection, str):
        import json as _json

        try:
            account_selection = _json.loads(account_selection)
        except (_json.JSONDecodeError, TypeError, ValueError) as e:
            log.warning("_exec_bot_factory: invalid account_selection=%r, using all accounts: %s", account_selection, e)
            account_selection = {}
    selected_acc_ids = account_selection.get("account_ids") or []

    accounts_rows = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=selected_acc_ids or None,
        respect_cooldown=False,
        action_type="create_bot",
    )

    if not accounts_rows:
        await _safe_execute(
                pool,
            "UPDATE global_presence_plans SET status='failed', updated_at=now() WHERE id=$1",
            plan_id,
        )
        return {"status": "failed", "reason": "no active accounts found"}

    # Отказной захват всего пула: дальше аккаунты выбираются из него по ходу
    # (в т.ч. запасной при карантине), поэтому захватываем пул целиком.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts_rows])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts_rows) - len(claimed_ids)
    accounts_rows = [a for a in accounts_rows if int(a["id"]) in set(claimed_ids)]

    try:

        # Build lookup by id for per-target account assignment (mirrors _exec_global_presence_channel)
        acc_by_id = {a["id"]: dict(a) for a in accounts_rows}
        # Fallback list for round-robin when target has no selected_account_id
        accounts_list = list(accounts_rows)

        # То же расклинивание, что у каналов: брошенная цель иначе не
        # рассматривается никогда, и план застывает недостроенным.
        await _revive_abandoned_gp_targets(pool, plan_id, "bot")

        targets = await _safe_fetch(
                pool,
            "SELECT * FROM global_presence_targets WHERE plan_id=$1 AND status='pending' ORDER BY id",
            plan_id,
        )
        if not targets:
            await _safe_execute(
                    pool,
                "UPDATE global_presence_plans SET status='done', updated_at=now() WHERE id=$1",
                plan_id,
            )
            return {"status": "done", "created": 0, "failed": 0, "plan_id": plan_id}

        await _safe_execute(
                pool,
            "UPDATE global_presence_plans SET status='running', updated_at=now() WHERE id=$1",
            plan_id,
        )

        created_count = 0
        failed_count = 0
        acc_rr_idx = 0  # round-robin index for fallback only
        total = len(targets)
        _gp_bot_eco_id: int | None = None  # lazily loaded from plan
        await _safe_execute(
                pool,
            "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
        )

        for i, target in enumerate(targets):
            if await _is_cancelled(pool, op_id):
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1",
                    plan_id,
                )
                return {
                    "status": "cancelled",
                    "created": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }

            # Use per-target assigned account; fall back to round-robin if not set
            acc_id = target["selected_account_id"]
            acc = acc_by_id.get(acc_id) if acc_id else None
            if not acc:
                acc = dict(accounts_list[acc_rr_idx % len(accounts_list)])
                acc_rr_idx += 1

            bot_name = target["planned_name"] or f"Bot {i + 1}"
            bot_username = (target["planned_username"] or "").lstrip("@")
            # Ensure bot username ends with _bot
            if bot_username and not bot_username.lower().endswith("bot"):
                bot_username = bot_username + "_bot"

            # Atomic claim: skip if already claimed by another worker
            claimed = await _safe_execute(
                    pool,
                "UPDATE global_presence_targets SET status='running' WHERE id=$1 AND status='pending'",
                target["id"],
            )
            if claimed == "UPDATE 0":
                log.info(
                    "op_worker gp_bot: target %d already claimed, skipping", target["id"]
                )
                continue
            await session_simulator.typing_delay(bot_name)

            t0_gp_bot = time.monotonic()
            result = await account_manager.create_bot_via_botfather(
                acc["session_str"], bot_name, bot_username or f"geo_{i + 1}_bot", _acc=acc
            )

            # BotFather flood_wait — ждём указанное время и пробуем другим аккаунтом
            if result.get("error") and result.get("flood_wait"):
                wait_s = int(result["flood_wait"]) + random.randint(30, 60)
                log.info(
                    "op_worker gp_bot: BotFather flood_wait %ds, switching account and retrying",
                    wait_s,
                )
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_targets SET status='pending' WHERE id=$1",
                    target["id"],
                )
                # Лимит BotFather считается на аккаунт, а ретрай идёт уже с
                # ДРУГОГО аккаунта — держать весь прогон всю паузу незачем.
                await bounded_flood_sleep(wait_s, "gp_bot")
                # Switch to next account for retry (use round-robin index over accounts_list)
                acc_rr_idx += 1
                acc = dict(accounts_list[acc_rr_idx % len(accounts_list)])
                result = await account_manager.create_bot_via_botfather(
                    acc["session_str"],
                    bot_name,
                    bot_username or f"geo_{i + 1}_bot",
                    _acc=acc,
                )

            if result.get("error"):
                _gp_bot_err = str(result["error"])
                if _is_dead_session_error(_gp_bot_err):
                    try:
                        await pool.execute(
                            """UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired',
                                   status_reason=$2 WHERE id=$1 AND is_active=TRUE""",
                            acc["id"], f"Dead session (gp_bot): {_gp_bot_err[:180]}",
                        )
                        log.warning("op_worker gp_bot: deactivated dead session account_id=%s", acc["id"])
                    except Exception as _dbe:
                        log.warning("op_worker gp_bot: deactivate failed: %s", _dbe)
                await _safe_execute(
                        pool,
                    "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                    _gp_bot_err[:500],
                    target["id"],
                )
                failed_count += 1
                _infra_mem.record_account_op(
                    acc["id"],
                    "global_presence_bot",
                    success=False,
                    error=_gp_bot_err[:100],
                )
                await _audit(
                    pool,
                    owner_id,
                    "gp_create_bot",
                    "error",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=bot_name[:100],
                    error_msg=_gp_bot_err[:200],
                )
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                await asyncio.sleep(random.uniform(30, 60))
                continue

            token = result.get("token", "")
            actual_username = result.get("username", bot_username)
            # Отмечаем созданного бота СРАЗУ, до записи в каталог: BotFather его
            # уже отдал, и повторное создание после обрыва завело бы ВТОРОГО.
            # По этой отметке расклинивание отличает созданное от неначатого.
            if token and ":" in token:
                try:
                    await _safe_execute(
                        pool,
                        "UPDATE global_presence_targets SET result_asset_id=$1 WHERE id=$2",
                        int(token.split(":")[0]), target["id"],
                        log_ctx=f"[gp_bot_mark_created target={target['id']}]",
                    )
                except (TypeError, ValueError):
                    log.debug("gp_bot: токен без числового id, отметка пропущена")

            # Save bot to managed_bots (token format: "{bot_id}:{hash}")
            try:
                from database import db as _db

                if token and ":" in token:
                    bot_id_int = int(token.split(":")[0])
                    await _db.add_bot(
                        pool, token, bot_id_int, actual_username, bot_name, owner_id, bot=bot
                    )
            except Exception as e:
                log.warning("op_worker gp_bot: managed_bots insert failed: %s", e)

            await _safe_execute(
                    pool,
                "UPDATE global_presence_targets SET status='done' WHERE id=$1", target["id"]
            )
            _infra_mem.record_account_op(
                acc["id"],
                "global_presence_bot",
                success=True,
                duration_s=time.monotonic() - t0_gp_bot,
            )
            await _audit(
                pool,
                owner_id,
                "gp_create_bot",
                "success",
                operation_id=op_id,
                account_id=acc["id"],
                target=(actual_username or bot_name)[:100],
                duration_ms=int((time.monotonic() - t0_gp_bot) * 1000),
            )

            # Add created bot to ecosystem
            try:
                if _gp_bot_eco_id is None:
                    _eco_row = await pool.fetchrow(
                        "SELECT ecosystem_id FROM global_presence_plans WHERE id=$1",
                        plan_id,
                    )
                    _gp_bot_eco_id = (_eco_row["ecosystem_id"] if _eco_row else None) or 0
                if _gp_bot_eco_id and token and ":" in token:
                    from services import ecosystem_brain as _eb

                    _bot_id_for_eco = int(token.split(":")[0])
                    await _eb.add_member(
                        pool, _gp_bot_eco_id, owner_id, "bot", _bot_id_for_eco
                    )
                    await _eb.add_member(
                        pool, _gp_bot_eco_id, owner_id, "account", acc["id"]
                    )
            except Exception as e:
                log_exc_swallow(log, f"gp_bot ecosystem add_member to plan {plan_id} failed: {e}")

            await _safe_execute(
                    pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                op_id,
                created_count + failed_count + 1,
                f"{target.get('city', '?')} → @{actual_username}",
                f"bot created: @{actual_username}",
            )
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            created_count += 1

            if created_count % 5 == 0:
                try:
                    await db.notify_if_enabled(
                        pool,
                        bot,
                        owner_id,
                        "op_complete",
                        f"🤖 <b>Создание ботов (план #{plan_id}):</b> {created_count + failed_count}/{total}\n"
                        f"✅ Создано: {created_count} | ❌ Ошибок: {failed_count}",
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"Сбой отправки прогресса создания ботов плана #{plan_id} владельцу {owner_id}",
                    )

            # Humanized delay between BotFather interactions (под губернатором темпа)
            await asyncio.sleep(await _governed_delay(
                pool, owner_id, random.uniform(60, 120) * session_simulator.chaos_factor()))

        final_status = (
            "done" if failed_count == 0 else ("failed" if created_count == 0 else "done")
        )
        await _safe_execute(
                pool,
            "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
            final_status,
            plan_id,
        )
        return {
            "status": "done",
            "created": created_count,
            "failed": failed_count,
            "plan_id": plan_id,
            "summary": f"Создано ботов: {created_count}, ошибок: {failed_count}",
        }
    finally:
        await release_accounts(claimed_ids)


_MIN_ACCOUNT_AGE_DAYS = 14  # минимальный возраст аккаунта в системе для bulk-операций
_MIN_TRUST_SCORE = 0.35  # минимальный trust_score для создания каналов
_MAX_CHANNELS_PER_DAY = 2  # максимум каналов в сутки с одного аккаунта

_BULK_PACING_PRESETS: dict[str, dict] = {
    "safe":   {"item_delay": (90, 150), "cooldown_every": 5, "cooldown_delay": (300, 600)},
    "medium": {"item_delay": (45, 90),  "cooldown_every": 5, "cooldown_delay": (120, 300)},
    "fast":   {"item_delay": (30, 60),  "cooldown_every": 5, "cooldown_delay": (120, 300)},
    "turbo":  {"item_delay": (45, 90),  "cooldown_every": 3, "cooldown_delay": (180, 360)},
}


async def _exec_bulk_create_channels_multi(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Multi-account round-robin bulk channel creation (from UI handler via operation_bus)."""
    from services import account_manager

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    if not account_ids:
        return {"status": "failed", "reason": "Не указаны account_ids"}
    title_base = params.get("title", "Channel")
    name_mode = params.get("name_mode", "none")
    channel_count = int(params.get("channel_count", 1))
    about = params.get("about", "")
    is_group = bool(params.get("is_group", False))
    bulk_pacing = params.get("bulk_pacing", "medium")

    preset = _BULK_PACING_PRESETS.get(bulk_pacing, _BULK_PACING_PRESETS["medium"])

    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    active_accounts = [dict(r) for r in rows]
    if not active_accounts:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    # Отказной захват: работаем ТОЛЬКО с реально захваченными сессиями. Раньше
    # здесь был безусловный mark_accounts_in_use — он не спрашивал арбитра, и
    # занятый аккаунт всё равно шёл в работу (вторая сессия → AUTH_KEY_DUPLICATED).
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in active_accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(active_accounts) - len(claimed_ids)
    active_accounts = [a for a in active_accounts if int(a["id"]) in set(claimed_ids)]

    # Итоги считаем ПОСЛЕ захвата — иначе прогресс-бар обещал бы работу на
    # аккаунтах, которые нам не достались.
    total_ops = len(active_accounts) * channel_count
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total_ops, op_id)

    created_count = 0
    failed_count = 0
    global_idx = 1

    # SEO-массив: имена и @юзернеймы перестановкой ключей (не «Канал #1»).
    # name_mode == "keywords" → title_base трактуется как набор ключевых слов;
    # username_template (если задан) → публичные @юзернеймы вращением ключей.
    from services import name_variator
    _seo = name_mode == "keywords"
    _titles = name_variator.generate_titles(title_base, total_ops, seed=op_id) if _seo else None
    _uname_tpl = (params.get("username_template") or "").strip()
    _uname_gen = (name_variator.username_candidates(_uname_tpl, seed=op_id)
                  if _uname_tpl else None)

    # Автопосев + первый контент (настраивается ДО старта, чтобы не делать руками).
    _first_post = (params.get("first_post") or "").strip()
    _pin_first = bool(params.get("pin_first_post"))
    _seed_count = max(0, min(int(params.get("seed_count") or 0), 200))
    # Оживление первого поста: сколько аккаунтов ставят реакцию + дают просмотр.
    _engage_count = max(0, min(int(params.get("engage_count") or 0), 200))
    # Пул аккаунтов для посева — весь активный флот владельца (не только создатели),
    # чтобы в новый ресурс заходили ДРУГИЕ аккаунты. Берём один раз.
    _seed_pool: list[int] = []
    if _seed_count > 0:
        try:
            _seed_rows = await resource_selector.select_all_active(
                pool, owner_id, min_trust_score=0.0)
            _seed_pool = [int(r["id"]) for r in (_seed_rows or [])]
        except Exception:
            _seed_pool = []

    try:
        for task_i in range(total_ops):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "created": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }

            if not active_accounts:
                failed_count += total_ops - task_i
                break

            acc = active_accounts[task_i % len(active_accounts)]
            acc_label = acc.get("first_name") or acc.get("phone") or str(acc["id"])

            if _seo and _titles:
                title = _titles[task_i]
            elif name_mode == "num":
                title = f"{title_base} {global_idx}"
            elif name_mode == "acc":
                title = f"{title_base} ({acc_label[:20]})"
            else:
                title = title_base

            await session_simulator.typing_delay(title)

            # SEO-описание: разнообразим по ресурсам вариантами {а|б} (spintax).
            _about_i = about
            if about:
                try:
                    from services.dm_engine import expand_spintax as _spin_ab
                    _about_i = _spin_ab(about) or about
                except Exception:
                    _about_i = about

            result = await account_manager.create_channel(
                acc["session_str"], title, about=_about_i, megagroup=is_group, _acc=acc
            )

            flood_wait = result.get("flood_wait", 0) if isinstance(result, dict) else 0
            if result.get("banned") or account_manager.is_dead_session_error(
                result.get("error") if isinstance(result, dict) else str(result)
            ):
                await db.deactivate_account(pool, acc["id"], "banned/dead in bulk_create_channels")
                active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
                failed_count += 1
            elif isinstance(result, dict) and result.get("channel_id") and not result.get("error"):
                ch_id = result["channel_id"]
                # Публичный @юзернейм из вращения ключей. Занятые/невалидные
                # Telegram отвергает — берём следующий кандидат (несколько попыток).
                _assigned_username = None
                if _uname_gen is not None:
                    _tries = 0
                    for _cand in _uname_gen:
                        _tries += 1
                        if _tries > 5:        # не жжём аккаунт перебором @
                            break
                        _uerr = await account_manager.set_channel_username(
                            acc["session_str"], ch_id, _cand, _acc=acc)
                        if not _uerr:
                            _assigned_username = _cand
                            break
                        _uup = _uerr.upper()
                        if "FLOOD" in _uup or "TOO MANY" in _uup or "WAIT" in _uup:
                            break            # флуд — не жжём кандидаты, оставим без @
                        if "OCCUPIED" in _uup or "INVALID" in _uup or "TAKEN" in _uup:
                            continue          # занят/невалиден — следующий вариант
                        break                 # прочее — не зацикливаемся
                try:
                    await pool.execute(
                        """INSERT INTO managed_channels
                               (owner_id, acc_id, channel_id, title, username, access_hash, type)
                           VALUES ($1,$2,$3,$4,$5,$6,$7)
                           ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4, username=$5""",
                        owner_id, acc["id"], ch_id, title,
                        _assigned_username or result.get("username") or None,
                        result.get("access_hash", 0) or 0,
                        result.get("type", "channel"),
                    )
                except Exception:
                    log_exc_swallow(log, "bulk_create_channels_multi: managed_channels insert failed")

                # Автозаведение ключа для замера позиций: имя ресурса = его запрос.
                if _seo:
                    try:
                        from services import channel_ranking
                        await channel_ranking.register_for_channel(pool, owner_id, ch_id, title)
                    except Exception:
                        log_exc_swallow(log, "bulk_create_channels_multi: kw register failed")

                _ch_hash = int(result.get("access_hash", 0) or 0)
                # ── Первый контент: пост от создателя (он админ) + закреп ──────
                _first_msg_id = 0
                if _first_post:
                    try:
                        from services.dm_engine import expand_spintax as _spin
                        _text = _spin(_first_post) or _first_post   # разнообразим по каналам
                        _pr = await account_manager.post_to_channel(
                            acc["session_str"], ch_id, _text[:4000],
                            access_hash=_ch_hash,
                            username=(_assigned_username or ""), _acc=acc)
                        if isinstance(_pr, dict) and _pr.get("msg_id"):
                            _first_msg_id = int(_pr["msg_id"])
                        if _pin_first and _first_msg_id:
                            await account_manager.pin_last_channel_post(
                                acc["session_str"], ch_id, access_hash=_ch_hash,
                                username=(_assigned_username or ""), _acc=acc)
                    except Exception:
                        log_exc_swallow(log, "bulk_create_channels_multi: first_post failed")

                # ── Оживление первого поста: реакции + просмотры флотом ────────
                # Свежий пост с реакциями/просмотрами выглядит живым (лучше для
                # удержания и ранжирования). Только для публичного ресурса (@),
                # чтобы аккаунты видели пост без вступления. Через шину (boost_*).
                if _engage_count > 0 and _first_msg_id and _assigned_username and _seed_pool:
                    _eng_ids = [i for i in _seed_pool if i != int(acc["id"])][:_engage_count]
                    if _eng_ids:
                        try:
                            from services import operation_bus
                            await operation_bus.submit(
                                pool, owner_id, "boost_reactions",
                                {"channel": f"@{_assigned_username}", "msg_id": _first_msg_id,
                                 "account_ids": _eng_ids},
                                total_items=len(_eng_ids),
                                label=f"Оживление: реакции в {title[:40]}")
                            await operation_bus.submit(
                                pool, owner_id, "boost_views",
                                {"channel": f"@{_assigned_username}", "msg_ids": [_first_msg_id],
                                 "account_ids": _eng_ids},
                                total_items=len(_eng_ids),
                                label=f"Оживление: просмотры в {title[:40]}")
                        except Exception:
                            log_exc_swallow(log, "bulk_create_channels_multi: engage submit failed")

                # ── Автопосев участников: отдельной операцией через шину ───────
                # (проверенный путь boost_subscribers: свой захват, карантин,
                # пейсинг). Заводим ДРУГИЕ аккаунты флота в новый ресурс.
                if _seed_count > 0 and _seed_pool:
                    _seed_target = (f"@{_assigned_username}" if _assigned_username else "")
                    if not _seed_target:
                        # приватный/без @ — берём инвайт-ссылку от создателя
                        try:
                            _lk = await account_manager.create_channel_invite_link(
                                acc["session_str"], ch_id, access_hash=_ch_hash, _acc=acc)
                            _seed_target = (_lk.get("invite_link")
                                            if isinstance(_lk, dict) else "") or ""
                        except Exception:
                            _seed_target = ""
                    _seed_ids = [i for i in _seed_pool if i != int(acc["id"])][:_seed_count]
                    if _seed_target and _seed_ids:
                        try:
                            from services import operation_bus
                            await operation_bus.submit(
                                pool, owner_id, "boost_subscribers",
                                {"target": _seed_target, "account_ids": _seed_ids},
                                total_items=len(_seed_ids),
                                label=f"Автопосев: +{len(_seed_ids)} в {title[:40]}")
                        except Exception:
                            log_exc_swallow(log, "bulk_create_channels_multi: seed submit failed")

                try:
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message)"
                        " VALUES($1,$2,$3,'ok',$4)",
                        op_id, task_i + 1, title, f"channel_id={ch_id}",
                    )
                except Exception:
                    log_exc_swallow(log, "bulk_create_channels_multi: operation_log insert failed")
                created_count += 1
            else:
                err_msg = str(result.get("error", result) if isinstance(result, dict) else result)[:200]
                try:
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message)"
                        " VALUES($1,$2,$3,'error',$4)",
                        op_id, task_i + 1, title, err_msg,
                    )
                except Exception:
                    log_exc_swallow(log, "bulk_create_channels_multi: operation_log error insert failed")
                failed_count += 1

            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            global_idx += 1

            if task_i < total_ops - 1:
                cooldown_every = preset["cooldown_every"]
                if (task_i + 1) % cooldown_every == 0:
                    delay = random.uniform(*preset["cooldown_delay"])
                else:
                    delay = random.uniform(*preset["item_delay"])
                chaos = session_simulator.chaos_factor()
                await bounded_flood_sleep(
                    max(delay * chaos, flood_wait), "bulk_create_channels")
    finally:
        await release_accounts(claimed_ids)

    return {
        "status": "done",
        "created": created_count,
        "failed": failed_count,
        "summary": (f"Создано каналов: {created_count}, ошибок: {failed_count}"
                    + (f", пропущено занятых аккаунтов: {_busy}" if _busy else "")),
    }


async def _exec_check_channel_rankings(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Замер позиций каналов/чатов владельца в поиске Telegram по ключам."""
    from services import channel_ranking
    res = await channel_ranking.check_owner(pool, owner_id, bot=bot)
    if res.get("error"):
        return {"status": "failed", "summary": f"⚠️ {res['error']}"}
    return {"status": "done",
            "checked": res.get("checked", 0), "found": res.get("found", 0),
            "summary": (f"Проверено ключей: {res.get('checked', 0)}, "
                        f"в выдаче найдено: {res.get('found', 0)}")}


async def _exec_bulk_create_channels(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое создание каналов через Telethon с умными задержками.

    Поддерживает два режима:
    - multi-account: account_ids, title, name_mode, channel_count, bulk_pacing, is_group
    - legacy single-account: acc_id, prefix, count, about, username_pattern
    """
    from services import account_manager
    from datetime import datetime, timezone

    # ── Multi-account mode (new handler path via operation_bus) ───────────────
    if params.get("account_ids"):
        return await _exec_bulk_create_channels_multi(pool, bot, op_id, owner_id, params)

    # ── Legacy single-account mode ─────────────────────────────────────────────
    prefix = params.get("prefix", "Channel")
    count = int(params.get("count", 5))
    about = params.get("about", "")
    username_pattern = params.get("username_pattern", "")
    acc_id = params.get("acc_id", 0)
    # Group Factory passes is_group=True to create a supergroup instead of a
    # broadcast channel. Without honouring it, create_group silently produced a
    # channel (wrong entity type).
    is_group = bool(params.get("is_group", False))

    # Get the account via resource_selector (flood-aware)
    if acc_id:
        candidates = await resource_selector.select_all_active(
            pool, owner_id, include_ids=[acc_id], respect_cooldown=False
        )
        acc_row = candidates[0] if candidates else None
        acc = dict(acc_row) if acc_row else None
    else:
        acc = await resource_selector.select_account(pool, owner_id, "create_channel")

    if not acc:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    # Отказной захват: создание каналов идёт живой сессией аккаунта. Без захвата
    # он мог параллельно вести другую операцию → две сессии на одном auth-key.
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "reason": "Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        # ── Account health gate ───────────────────────────────────────────────────
        acc_data = await _safe_fetchrow(
                pool,
            "SELECT added_at, trust_score FROM tg_accounts WHERE id=$1", acc["id"]
        )
        if acc_data:
            added_at = acc_data["added_at"]
            trust_score = float(acc_data["trust_score"] or 0.5)
            if added_at:
                age_days = (
                    datetime.now(timezone.utc) - added_at.replace(tzinfo=timezone.utc)
                ).days
                if age_days < _MIN_ACCOUNT_AGE_DAYS:
                    return {
                        "status": "failed",
                        "reason": (
                            f"Аккаунт добавлен {age_days} дн. назад — требуется минимум {_MIN_ACCOUNT_AGE_DAYS} дней. "
                            "Сначала прогрейте аккаунт через раздел 🌱 Прогрев."
                        ),
                    }
            if trust_score < _MIN_TRUST_SCORE:
                return {
                    "status": "failed",
                    "reason": (
                        f"Низкий trust_score аккаунта ({trust_score:.2f}). "
                        "Требуется прогрев перед bulk-операциями."
                    ),
                }

        # ── Daily channel creation cap (soft warning only, не блокируем) ─────────
        created_today = await _safe_fetchval(
                pool,
            """SELECT COUNT(*) FROM managed_channels
               WHERE acc_id=$1 AND owner_id=$2
                 AND added_at >= now() - INTERVAL '24 hours'""",
            acc["id"],
            owner_id,
        )
        if (created_today or 0) >= _MAX_CHANNELS_PER_DAY:
            log.warning(
                "op_worker bulk_channels: daily cap reached acc=%s created_today=%s requested=%s",
                acc["id"],
                created_today,
                count,
            )
            return {
                "status": "failed",
                "reason": (
                    f"Аккаунт уже создал {created_today} канал(ов) за последние 24ч. "
                    f"Безопасный лимит: {_MAX_CHANNELS_PER_DAY}/день. "
                    "Используйте другой аккаунт или подождите."
                ),
            }
        created_count = 0
        failed_count = 0

        for i in range(count):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "created": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }

            num = i + 1
            # Single-item creation (Factory) uses the title verbatim; only bulk runs
            # get a "#N" suffix to keep names unique.
            title = prefix if count == 1 else f"{prefix} #{num}"
            if username_pattern:
                username = f"{username_pattern}_{num}"
            else:
                username = ""

            # Human-like typing delay
            await session_simulator.typing_delay(title)

            result = await account_manager.create_channel(
                acc["session_str"], title, about=about, megagroup=is_group, _acc=acc
            )

            # Handle flood wait
            if result.get("error") and result.get("flood_wait"):
                raw_flood = int(result["flood_wait"])
                if raw_flood > 600:
                    # Flood wait too long to block the batch — skip this channel
                    log.warning(
                        "op_worker bulk_channels: flood wait %ds too long — skipping",
                        raw_flood,
                    )
                else:
                    wait_time = raw_flood + 15
                    log.info("op_worker bulk_channels: flood %ds, sleeping...", wait_time)
                    await asyncio.sleep(wait_time)
                    result = await account_manager.create_channel(
                        acc["session_str"], title, about=about, megagroup=is_group, _acc=acc
                    )

            if (
                isinstance(result, dict)
                and result.get("channel_id")
                and not result.get("error")
            ):
                ch_id = result["channel_id"]
                # Save to managed_channels. Персистим access_hash, возвращённый
                # create_channel: без него публикация (bulk_post_chans/mass_publish)
                # вынуждена дорезолвивать peer лишними API-вызовами, а для приватного
                # канала без username — рискует не найти entity. Импорт каналов тоже
                # сохраняет access_hash — приводим создание к тому же контракту.
                ch_type = result.get("type") or ("group" if is_group else "channel")
                ch_hash = int(result.get("access_hash") or 0)
                await _safe_execute(
                        pool,
                    """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username, access_hash, type)
                       VALUES($1,$2,$3,$4,$5,$6,$7)
                       ON CONFLICT(owner_id, channel_id) DO UPDATE
                       SET title=$4, access_hash=$6, type=$7""",
                    owner_id,
                    acc["id"],
                    ch_id,
                    title,
                    username or None,
                    ch_hash,
                    ch_type,
                )
                # Set username if pattern provided — 60-120s delay prevents geo-ban detection
                if username:
                    await asyncio.sleep(random.uniform(30, 60))
                    err = await account_manager.set_channel_username(
                        acc["session_str"], ch_id, username, _acc=acc
                    )
                    if err:
                        log.info(
                            "op_worker bulk_channels: username '%s' failed (%s), trying variants",
                            username,
                            err[:80],
                        )
                        # Try up to 3 variants: add numeric suffix
                        for suffix in (
                            f"_{i + 1}",
                            f"_{i + 1}x",
                            f"_{i + 1}_{random.randint(10, 99)}",
                        ):
                            variant = username.rstrip("_") + suffix
                            await asyncio.sleep(random.uniform(5, 10))
                            err2 = await account_manager.set_channel_username(
                                acc["session_str"], ch_id, variant, _acc=acc
                            )
                            if not err2:
                                log.info(
                                    "op_worker bulk_channels: variant '%s' accepted",
                                    variant,
                                )
                                err = None
                                break
                        if err:
                            log.info(
                                "op_worker bulk_channels: all username variants failed, channel created without username"
                            )

                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                    op_id,
                    num,
                    f"{title}",
                    f"channel_id={ch_id}" + (f" @{username}" if username else ""),
                )
                created_count += 1
            else:
                err_msg = result if isinstance(result, str) else str(result)
                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id,
                    num,
                    f"{title}",
                    err_msg[:200],
                )
                failed_count += 1

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            if i < count - 1:
                tod_factor = session_simulator.time_of_day_factor()
                chaos = session_simulator.chaos_factor()
                if i % 5 == 4:
                    cooldown = random.uniform(120, 240) * chaos * tod_factor
                    log.info(
                        "op_worker bulk_channels: cooldown %.0fs after %d items",
                        cooldown,
                        i + 1,
                    )
                    await asyncio.sleep(await _governed_delay(pool, owner_id, cooldown))
                else:
                    delay = random.uniform(15, 30) * chaos * tod_factor
                    await asyncio.sleep(await _governed_delay(pool, owner_id, delay))

            # Progress update every 5 channels
            if created_count > 0 and created_count % 5 == 0:
                try:
                    await db.notify_if_enabled(
                        pool,
                        bot,
                        owner_id,
                        "op_complete",
                        f"📡 <b>Массовое создание каналов #{op_id}:</b> {created_count + failed_count}/{count}\n"
                        f"✅ Создано: {created_count} | ❌ Ошибок: {failed_count}",
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"Сбой отправки прогресса массового создания каналов #{op_id} владельцу {owner_id}",
                    )

        _unit = "групп" if is_group else "каналов"
        return {
            "status": "done",
            "created": created_count,
            "failed": failed_count,
            "summary": f"Создано {_unit}: {created_count}, ошибок: {failed_count}",
        }
    finally:
        await release_accounts([_claimed_acc])


async def _exec_bot_factory_multi(
    pool: asyncpg.Pool,
    bot: Bot,
    op_id: int,
    owner_id: int,
    params: dict,
    account_ids: list[int],
) -> dict:
    """Массовое создание ботов — несколько аккаунтов, round-robin с fallback."""
    from services import account_manager
    from services.username_engine import unique_bot_username

    bot_count = max(1, min(int(params.get("bot_count", 1)), 10))
    bot_name = (params.get("bot_name") or "Bot").strip()
    base_username = (params.get("base_username") or "").strip().lstrip("@")

    # Одна дверь: выбранные аккаунты через флуд-осознанный select_all_active
    # (фильтр cooldown/мёртвых статусов + полный транспорт с cf_relay_url).
    from services import resource_selector as _rsel
    rows = await _rsel.select_all_active(
        pool, owner_id, include_ids=account_ids, min_trust_score=0.0)
    active_accounts = [dict(r) for r in rows]
    if not active_accounts:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов для Bot Factory"}

    # Отказной захват вместо безусловной пометки: занятую сессию в работу не берём.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in active_accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⚠️ Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(active_accounts) - len(claimed_ids)
    active_accounts = [a for a in active_accounts if int(a["id"]) in set(claimed_ids)]
    total = len(active_accounts) * bot_count
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    created_count = 0
    failed_count = 0
    created_tokens: list[str] = []

    # SEO-массив для ботов: имя из ключей (перестановка), @username вращением
    # ключей с суффиксом bot. Иначе — прежняя нумерация unique_bot_username.
    from services import name_variator as _nv
    _bf_seo = params.get("name_mode") == "keywords"
    _bf_titles = _nv.generate_titles(bot_name, total, seed=op_id) if _bf_seo else None
    _bf_ugen = (_nv.username_candidates(base_username, seed=op_id, require_suffix="bot")
                if (_bf_seo and base_username) else None)

    try:
        for global_i in range(total):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }
            if not active_accounts:
                break

            if _bf_ugen is not None:
                username = next(_bf_ugen, None) or (unique_bot_username(base_username, global_i)
                                                    if base_username else f"bot{random.randint(10000,99999)}bot")
            else:
                username = unique_bot_username(base_username, global_i) if base_username else f"bot{random.randint(10000, 99999)}bot"
            if _bf_seo and _bf_titles:
                display_name = _bf_titles[global_i]
            else:
                display_name = f"{bot_name} {global_i + 1}" if total > 1 else bot_name

            await session_simulator.typing_delay(display_name)
            result = None
            tried: set[int] = set()
            for candidate in active_accounts:
                if candidate["id"] in tried:
                    continue
                tried.add(candidate["id"])
                result = await account_manager.create_bot_via_botfather(
                    candidate["session_str"],
                    bot_display_name=display_name,
                    bot_username=username,
                    _acc=candidate,
                )
                if result.get("banned") or account_manager.is_dead_session_error(result.get("error")):
                    await pool.execute(
                        "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", candidate["id"]
                    )
                    active_accounts = [a for a in active_accounts if a["id"] != candidate["id"]]
                    continue
                if result.get("peer_flood") or result.get("flood_wait"):
                    continue
                break
            if result is None:
                result = {"error": "нет доступных аккаунтов"}

            if result.get("token"):
                token = result["token"]
                actual_uname = result.get("username", username)
                created_tokens.append(token)
                bot_id = 0
                try:
                    import aiohttp as _aiohttp
                    async with _aiohttp.ClientSession() as _sess:
                        async with _sess.get(
                            f"https://api.telegram.org/bot{token}/getMe",
                            timeout=_aiohttp.ClientTimeout(total=10),
                        ) as _resp:
                            data = await _resp.json()
                            if data.get("ok"):
                                bot_id = data["result"]["id"]
                                actual_uname = data["result"].get("username", actual_uname)
                except Exception as e:
                    # Хвост токена — это кусок секрета. В лог только id бота.
                    log.warning("bot_factory_multi getMe failed for %s: %s",
                                mask_bot_token(token), redact_secrets(str(e)))
                if not bot_id:
                    failed_count += 1
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                    )
                    if global_i < total - 1:
                        await asyncio.sleep(1)
                    continue
                try:
                    from services.token_vault import encrypt_token as _enc_tok_mf
                    await pool.execute(
                        """INSERT INTO managed_bots(added_by, token, bot_id, username, first_name, is_active, acc_id)
                           VALUES($1,$2,$3,$4,$5,TRUE,$6)
                           ON CONFLICT(bot_id) DO UPDATE SET token=$2, username=$4, is_active=TRUE, acc_id=$6""",
                        owner_id, _enc_tok_mf(token), bot_id, actual_uname, display_name,
                        candidate.get("id"),
                    )
                except Exception as e:
                    log.warning("_exec_bot_factory_multi: managed_bots upsert failed bot_id=%s: %s", bot_id, e)
                created_count += 1
            else:
                failed_count += 1

            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            if global_i < total - 1:
                chaos = session_simulator.chaos_factor()
                tod = session_simulator.time_of_day_factor()
                pause = (random.uniform(120, 240) if global_i % 3 == 2 else random.uniform(45, 90)) * chaos * tod
                await asyncio.sleep(pause)
    finally:
        await release_accounts(claimed_ids)

    return {
        "status": "done",
        "ok": created_count,
        "failed": failed_count,
        "created_tokens": created_tokens[:10],
        "summary": (f"Создано ботов: {created_count}, ошибок: {failed_count}"
                    + (f", пропущено занятых аккаунтов: {_busy}" if _busy else "")),
    }


async def _exec_bot_factory(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать ботов через @BotFather FSM с умными задержками.

    Params (single-account):
      acc_id         — int, id аккаунта в tg_accounts
      count          — int, количество ботов (1-10)
      name_template  — str, шаблон имени: "My Bot" → "My Bot 1", "My Bot 2"...
      uname_template — str, шаблон username: "mybot" → "mybot1_bot", "mybot2_bot"...

    Params (multi-account round-robin):
      account_ids    — list[int], несколько аккаунтов
      bot_count      — int, ботов на аккаунт
      bot_name       — str, отображаемое имя
      base_username  — str, базовый username
    """
    from services import account_manager

    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    if account_ids:
        return await _exec_bot_factory_multi(pool, bot, op_id, owner_id, params, account_ids)

    count = max(1, min(int(params.get("count", 1)), 10))
    name_tpl = (params.get("name_template") or "Bot").strip()
    uname_tpl = (params.get("uname_template") or "").strip().lstrip("@")
    acc_id = params.get("acc_id", 0)

    if acc_id:
        candidates = await resource_selector.select_all_active(
            pool, owner_id, include_ids=[int(acc_id)], respect_cooldown=False
        )
        acc_row = candidates[0] if candidates else None
        acc = dict(acc_row) if acc_row else None
    else:
        acc = await resource_selector.select_account(pool, owner_id, "bot_factory")

    if not acc:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов для Bot Factory"}

    # Отказной захват: операция работает живой сессией аккаунта. Без захвата он
    # мог параллельно вести другую операцию → две сессии на одном auth-key.
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        created_count = 0
        failed_count = 0
        created_tokens: list[str] = []

        # SEO-массив для ботов: имя из ключей (перестановка), @username вращением
        # ключей с обязательным суффиксом bot. Иначе — прежнее «Имя 1, Имя 2».
        from services import name_variator as _nv
        _bf_seo = params.get("name_mode") == "keywords"
        _bf_titles = _nv.generate_titles(name_tpl, count, seed=op_id) if _bf_seo else None
        _bf_ugen = (_nv.username_candidates(uname_tpl, seed=op_id, require_suffix="bot")
                    if (_bf_seo and uname_tpl) else None)

        for i in range(count):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": created_count,
                    "failed": failed_count,
                    "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
                }

            num = i + 1
            if _bf_seo and _bf_titles:
                display_name = _bf_titles[i]
            else:
                display_name = f"{name_tpl} {num}" if count > 1 else name_tpl
            if _bf_ugen is not None:
                username_base = next(_bf_ugen, None) or (
                    f"{uname_tpl}{num}bot" if uname_tpl else f"bot{random.randint(10000,99999)}bot")
            else:
                username_base = f"{uname_tpl}{num}" if uname_tpl else f"bot{random.randint(10000, 99999)}"
                if not username_base.endswith("bot"):
                    username_base = username_base + "bot"

            await session_simulator.typing_delay(display_name)

            result = await account_manager.create_bot_via_botfather(
                acc["session_str"],
                bot_display_name=display_name,
                bot_username=username_base,
                _acc=acc,
            )

            if result.get("token"):
                token = result["token"]
                actual_uname = result.get("username", username_base)
                created_tokens.append(token)

                # Validate token and get bot_id
                bot_id = 0
                try:
                    import aiohttp as _aiohttp
                    async with _aiohttp.ClientSession() as _sess:
                        async with _sess.get(
                            f"https://api.telegram.org/bot{token}/getMe",
                            timeout=_aiohttp.ClientTimeout(total=10),
                        ) as _resp:
                            data = await _resp.json()
                            if data.get("ok"):
                                bot_id = data["result"]["id"]
                                actual_uname = data["result"].get("username", actual_uname)
                except Exception:
                    log_exc_swallow(log, "_exec_bot_factory: getMe failed for token ***")

                # Save to managed_bots
                try:
                    from services.token_vault import encrypt_token as _enc_tok_f
                    await pool.execute(
                        """INSERT INTO managed_bots(added_by, token, bot_id, username, first_name, is_active, acc_id)
                           VALUES($1,$2,$3,$4,$5,TRUE,$6)
                           ON CONFLICT(bot_id) DO UPDATE SET token=$2, username=$4, is_active=TRUE, acc_id=$6""",
                        owner_id,
                        _enc_tok_f(token),
                        bot_id or 0,
                        actual_uname,
                        display_name,
                        acc.get("id"),
                    )
                except Exception:
                    log_exc_swallow(log, "_exec_bot_factory: managed_bots upsert failed")

                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok',$4)",
                    op_id,
                    num,
                    display_name,
                    f"@{actual_uname}",
                )
                created_count += 1
                log.info(
                    "_exec_bot_factory op=%d: created @%s (bot_id=%s)",
                    op_id, actual_uname, bot_id,
                )
            else:
                err_msg = result.get("error", "unknown error")[:200]
                flood_wait = result.get("flood_wait")
                if flood_wait:
                    wait_secs = int(flood_wait)
                    if wait_secs <= 600:
                        log.info(
                            "_exec_bot_factory: FloodWait %ds, sleeping...", wait_secs + 15
                        )
                        await asyncio.sleep(wait_secs + 15)
                        # Retry once after flood wait
                        result2 = await account_manager.create_bot_via_botfather(
                            acc["session_str"],
                            bot_display_name=display_name,
                            bot_username=username_base,
                            _acc=acc,
                        )
                        if result2.get("token"):
                            token = result2["token"]
                            actual_uname = result2.get("username", username_base)
                            created_tokens.append(token)
                            try:
                                from services.token_vault import encrypt_token as _enc_tok_fr
                                _retry_bot_id = int(token.split(":")[0]) if ":" in token else 0
                                _enc_retry_tok = _enc_tok_fr(token)
                                await pool.execute(
                                    """INSERT INTO managed_bots(added_by, token, bot_id, username, first_name, is_active, acc_id)
                                       VALUES($1,$2,$3,$4,$5,TRUE,$6)
                                       ON CONFLICT(bot_id) DO UPDATE SET token=$2, username=$4, is_active=TRUE, acc_id=$6""",
                                    owner_id, _enc_retry_tok, _retry_bot_id, actual_uname, display_name,
                                    acc.get("id"),
                                )
                            except Exception as e:
                                log_exc_swallow(log, f"bot_factory retry managed_bots upsert failed: {e}")
                            created_count += 1
                            await _safe_execute(
                                    pool,
                                "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                                op_id, num, display_name, f"@{actual_uname} (retry ok)",
                            )
                            await _safe_execute(
                                    pool,
                                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                            )
                            await asyncio.sleep(random.uniform(30, 60))
                            continue

                failed_count += 1
                await _safe_execute(
                        pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id,
                    num,
                    display_name,
                    err_msg,
                )
                log.warning(
                    "_exec_bot_factory op=%d: failed to create '%s': %s",
                    op_id, display_name, err_msg,
                )

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            if i < count - 1:
                # Anti-flood: BotFather rate-limits bot creation aggressively
                chaos = session_simulator.chaos_factor()
                tod = session_simulator.time_of_day_factor()
                if i % 3 == 2:
                    # Longer pause every 3 bots
                    pause = random.uniform(120, 240) * chaos * tod
                else:
                    pause = random.uniform(45, 90) * chaos * tod
                await asyncio.sleep(pause)

        return {
            "status": "done",
            "ok": created_count,
            "failed": failed_count,
            "created_tokens": created_tokens[:10],
            "summary": f"Создано ботов: {created_count}, ошибок: {failed_count}",
        }
    finally:
        await release_accounts([_claimed_acc])


async def _exec_strike(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выполнить Strike-операцию через staggered_strike() из strike_engine.

    Параметры params:
      target        — username или ссылка (@channel или t.me/channel)
      reason        — причина жалобы (spam/violence/fraud/csam/...)
      preset        — пресет (content_spam/threat_real/fake_docs/...)
      num_waves     — количество волн для plan_waves() (default 3)
      account_ids   — конкретные id аккаунтов (опционально, если не указаны — авто)
      label         — метка операции (опционально)
    """
    from services.strike_engine import (
        StrikePlan,
        staggered_strike,
        format_strike_summary,
        preflight_accounts,
        plan_waves,
    )
    import time as _time

    target = params.get("target", "").strip()
    reason = params.get("reason", "spam")
    preset = params.get("preset") or None
    label = params.get("label") or f"queued_strike_{op_id}"
    # mode: сначала из params (явно), потом из strike_access (настройки пользователя)
    mode_from_params = params.get("mode", "")
    # Настойчивая эскалация: давить по расписанию, пока цель не снята или пока не
    # исчерпан лимит заходов. strike_chain — текущий номер захода (0 = первый).
    persist = bool(params.get("persist"))
    try:
        strike_chain = max(0, int(params.get("strike_chain") or 0))
    except (TypeError, ValueError):
        strike_chain = 0

    try:
        num_waves = max(1, int(params.get("num_waves", 3)))
    except (ValueError, TypeError):
        num_waves = 3

    account_ids: list[int] = []
    for _x in params.get("account_ids") or []:
        try:
            account_ids.append(int(_x))
        except (ValueError, TypeError):
            log.warning("_exec_strike op=%d: invalid account_id=%r, skipped", op_id, _x)

    if not target:
        return {"status": "failed", "summary": "⚠️ Strike: не указана цель (target)"}

    # ── Anti-detection (3A): не бить одну цель повторно в короткий интервал ────
    # Повторные удары по одной цели за короткое время — детекшн-паттерн и лишний
    # износ аккаунтов. is_strike_allowed матчит strike_history.target по тому же
    # значению, что пишется после удара (r.target == сырой target). Настраивается
    # min_restrike_hours (default 4ч), сознательный повтор — params.force=True.
    if not params.get("force"):
        try:
            _min_restrike_h = int(params.get("min_restrike_hours", 4))
        except (ValueError, TypeError):
            _min_restrike_h = 4
        if _min_restrike_h > 0:
            from services.strike_engine import is_strike_allowed

            if not await is_strike_allowed(pool, target, _min_restrike_h):
                log.info(
                    "_exec_strike op=%d: target=%s атакована в последние %dч — пропуск (anti-detect)",
                    op_id, target, _min_restrike_h,
                )
                return {
                    "status": "done",
                    "ok": 0,
                    "failed": 0,
                    "skipped": True,
                    "summary": (
                        f"⏳ Цель <code>{target}</code> уже атакована в последние "
                        f"{_min_restrike_h}ч — повтор пропущен для анти-детекции "
                        f"и защиты аккаунтов. Для сознательного повтора включите force."
                    ),
                }

    # ── Загрузить аккаунты через resource_selector (flood-aware + cooldown) ───
    raw_accounts = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=account_ids or None,
        respect_cooldown=False,  # preflight_accounts делает свою cooldown проверку
        action_type="strike",
    )

    # ВАЖНО (подход): отсутствие/недоступность TG-аккаунтов НЕ должно убивать
    # операцию. Массовые in-app жалобы — самый слабый вектор; реально удаляет канал
    # юридический вектор (письма abuse@/dmca@ Telegram, CSAM→NCMEC), а ему TG-
    # аккаунты не нужны — только SMTP. Раньше три ранних `return failed` (нет
    # аккаунтов / все в cooldown / все на прогреве) глушили операцию ДО юр-вектора,
    # т.е. единственный работающий вектор не запускался. Теперь пустой viable
    # допустим — ниже уйдём в legal-only, если настроен SMTP.
    accounts_dicts = [dict(a) for a in (raw_accounts or [])]

    # ── Pre-flight: фильтр cooldown + flood-state + сортировка ────────────────
    viable = preflight_accounts(accounts_dicts)

    # ── Warmup overlap guard: exclude accounts with active warmup plans ───────
    try:
        warming_ids: set[int] = set()
        _warmup_rows = await pool.fetch(
            "SELECT account_id FROM account_warmup_plans WHERE owner_id=$1 AND status='active'",
            owner_id,
        )
        warming_ids = {r["account_id"] for r in _warmup_rows}
        if warming_ids:
            before_count = len(viable)
            viable = [a for a in viable if a.get("id") not in warming_ids]
            excluded = before_count - len(viable)
            if excluded:
                log.warning(
                    "_exec_strike op=%d: excluded %d warmup accounts from strike",
                    op_id,
                    excluded,
                )
    except Exception:
        log_exc_swallow(log, f"_exec_strike op={op_id}: warmup overlap check failed")

    # ── Anti-detection (класс 7): риск-пульс ──────────────────────────────────
    # Strike — самая баноопасная операция (жалоба через реальный аккаунт). Бить с
    # аккаунта под недавним СЕРЬЁЗНЫМ ограничением (restriction_events) = быстрый
    # хард-бан. preflight выше ловит cooldown/flood, но НЕ критические restriction-
    # события с истёкшим cooldown. Общий гейт fail-open: все в карантине → НЕ
    # обнуляем (лучше рискнуть, чем no-op). Тот же гейт, что уже в mass_report.
    viable, _quar_skipped = await _filter_quarantined_accounts(pool, op_id, viable)
    if _quar_skipped:
        log.info(
            "_exec_strike op=%d: пропущено %d аккаунтов под риск-пульсом",
            op_id, _quar_skipped,
        )

    # ── Нет пригодных TG-аккаунтов → legal-only (не глушим операцию) ───────────
    # Юридический вектор (письма abuse@/dmca@ Telegram, CSAM→NCMEC) аккаунтов не
    # требует — и это ЕДИНСТВЕННЫЙ вектор, реально удаляющий канал. Запускаем его,
    # если настроен хотя бы один SMTP-ящик; иначе честно сообщаем, что запускать
    # нечего (ни аккаунтов, ни SMTP), а не тихо «готово, 0».
    if not viable:
        try:
            _smtp_cnt = int(await pool.fetchval(
                "SELECT COUNT(*) FROM strike_email_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE", owner_id) or 0)
        except Exception:
            _smtp_cnt = 0
        if not _smtp_cnt:
            return {
                "status": "failed",
                "summary": (
                    "⚠️ Strike: нет ни пригодных аккаунтов, ни SMTP-ящиков. "
                    "Подключите SMTP в настройках Strike — юридические письма "
                    "(abuse@/dmca@ Telegram) не требуют аккаунтов и это единственный "
                    "вектор, который реально удаляет канал."
                ),
            }
        log.info(
            "_exec_strike op=%d: нет TG-аккаунтов для in-app вектора — legal-only (SMTP=%d)",
            op_id, _smtp_cnt,
        )

    # ── Волны ─────────────────────────────────────────────────────────────────
    waves = plan_waves(viable, num_waves=num_waves)

    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
        len(viable),
        op_id,
    )

    # Определяем режим: явный из params > настройки пользователя > "normal"
    strike_mode = (
        mode_from_params if mode_from_params in ("fast", "normal", "maximum") else None
    )
    if not strike_mode:
        try:
            _mode_row = await pool.fetchrow(
                "SELECT mode FROM strike_access WHERE user_id=$1", owner_id
            )
            strike_mode = (_mode_row.get("mode") or "normal") if _mode_row else "normal"
        except Exception as e:
            log.warning('get_strike_plan_params: strike_mode lookup failed: %s', e)
            strike_mode = "normal"

    plan = StrikePlan(
        targets=[target],
        accounts=viable,
        reason=reason,
        preset=preset,
        label=label,
        # intel пустой: queued Strike не делает pre-recon.
        # staggered_strike безопасно обрабатывает intel={} — каждый аккаунт
        # самостоятельно вызывает GetFullChannel/GetHistory при выполнении.
        intel={},
        waves=waves,
        started_at=_time.time(),
        phase="recon",  # начальная фаза: strike_engine начнёт со сбора данных
        mode=strike_mode,
        owner_id=owner_id,
    )

    # Progress callback: обновляет done_items в БД при переходе между волнами
    # чтобы _progress_monitor мог отправлять уведомления 25/50/75%
    _wave_done = 0

    async def _strike_progress(phase: str, detail: str) -> None:
        nonlocal _wave_done
        if "wave" in phase.lower():
            _wave_done += 1
            pct_items = min(_wave_done * len(viable) // max(1, num_waves), len(viable))
            try:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                    pct_items,
                    op_id,
                )
            except Exception as e:
                log.warning("strike progress update failed for op %d: %s", op_id, e)

    try:
        results = await staggered_strike(plan, progress_cb=_strike_progress, pool=pool, op_id=op_id)
    except Exception as e:
        log.exception("op_worker _exec_strike #%d failed: %s", op_id, e)
        return {
            "status": "failed",
            "summary": f"❌ Strike завершился с ошибкой: {str(e)[:200]}",
        }

    await _safe_execute(
            pool,
        "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
        len(viable),
        op_id,
    )

    # ── Сохранение результатов в strike_history ───────────────────────────────
    # Queued strike (_exec_strike) не использует _strike_bg_v2, поэтому история
    # должна быть записана здесь — иначе результаты не видны в UI (History tab).
    for r in results:
        try:
            await pool.execute(
                """INSERT INTO strike_history(
                       owner_id, target, reason, preset,
                       accounts_used, peer_reported, msgs_reported, msgs_fetched,
                       pinned_reported, admins_reported, network_nodes, network_reports,
                       blocked, verified_down, duration_s, abuse_form_ok,
                       spambot_escalation,
                       accounts_ok, accounts_flood, accounts_banned, accounts_failed,
                       infra_domains, infra_apwg_sent, infra_registrar_sent)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                          $18,$19,$20,$21,$22,$23,$24)""",
                owner_id,
                r.target,
                reason,
                preset or None,
                r.unique_accounts,
                r.peer_reported,
                r.msgs_reported,
                getattr(r, "msgs_fetched", 0),
                r.pinned_reported,
                r.admins_reported,
                r.network_nodes,
                r.network_reports,
                r.blocked,
                r.verified_down,
                r.duration_s,
                r.abuse_form_ok,
                r.spambot_escalation,
                getattr(r, "accounts_ok", 0),
                getattr(r, "accounts_flood", 0),
                getattr(r, "accounts_banned", 0),
                getattr(r, "accounts_failed", 0),
                len(getattr(r, "infra_domains", []) or []),
                getattr(r, "infra_apwg_sent", 0),
                getattr(r, "infra_registrar_sent", 0),
            )
        except Exception as _he:
            log.warning(
                "_exec_strike op=%d: failed to write strike_history for target=%s: %s",
                op_id,
                r.target,
                _he,
            )

    summary_text = format_strike_summary(results)
    total_reported = sum(getattr(r, "peer_reported", 0) for r in results)

    # ── Настойчивая эскалация: переповтор, пока цель не снята ──────────────────
    # Реальные тейкдауны идут во времени: площадка/модерация реагирует не мгновенно.
    # Логика зависит от того, был ли флот в этом заходе:
    #   • с аккаунтами (можем верифицировать): продолжаем, только пока цель
    #     ПОДТВЕРЖДЁННО жива (verified_down False); None не гоним — не жжём флот вслепую;
    #   • legal-only (без аккаунтов): верификация невозможна, но это чистые письма —
    #     давление продолжается до лимита заходов даже без подтверждения.
    # Стоп всегда при подтверждённом снятии (verified_down True) или исчерпании лимита.
    if persist:
        _still_up = any(getattr(r, "verified_down", None) is False for r in results)
        _confirmed_down = results and all(
            getattr(r, "verified_down", None) is True for r in results)
        # Legal-only заход (без аккаунтов) НЕ может верифицировать цель — там некому
        # спросить у Telegram, жив ли канал (verified_down всегда None). Но это ЧИСТЫЕ
        # письма: флот не жжётся, риска нет. Поэтому в legal-only режиме продолжаем
        # давление до лимита заходов даже без подтверждения — юр-письма уходят каждый
        # заход. С флотом же (можем верифицировать) сохраняем строгую логику: гоним
        # только пока цель ПОДТВЕРЖДЁННО жива, не вслепую.
        _legal_only = not viable
        _keep_pressing = _still_up or (
            _legal_only and not _confirmed_down)
        if _confirmed_down:
            summary_text += "\n\n🎯 <b>Цель снята — эскалация остановлена.</b>"
        elif _keep_pressing and strike_chain + 1 < _MAX_STRIKE_CHAIN:
            _cont_id = await _schedule_strike_continuation(
                pool, owner_id, params, strike_chain + 1)
            if _cont_id:
                _how = ("юр-письма" if _legal_only else "давление")
                summary_text += (
                    f"\n\n🔁 <b>Настойчивая эскалация:</b> "
                    f"{'цель не подтверждена снятой' if _legal_only else 'цель ещё жива'} — "
                    f"следующий заход ({_how}) через ~{_STRIKE_CONTINUATION_HOURS}ч "
                    f"(попытка {strike_chain + 2}/{_MAX_STRIKE_CHAIN})."
                    + (" Проверить снятие без аккаунтов нельзя — письма идут до лимита."
                       if _legal_only else "")
                )
        elif _keep_pressing:
            summary_text += (
                f"\n\n🔁 <b>Настойчивая эскалация исчерпана</b> "
                f"({_MAX_STRIKE_CHAIN} заходов). "
                + ("Юр-письма отправлены во всех заходах; проверьте цель и усильте "
                   "пакетом жалобы (App Store/Google Play) вручную."
                   if _legal_only else
                   "Цель всё ещё активна — усильте пакетом жалобы вручную.")
            )

    return {
        "status": "done",
        "target": target,
        "waves_planned": num_waves,
        "accounts_used": len(viable),
        "total_reported": total_reported,
        "summary": summary_text or f"⚡ Strike по {target} завершён. Аккаунтов: {len(viable)}",
    }


async def _exec_network_broadcast(
    pool: asyncpg.Pool, bot: "Bot", op_id: int, owner_id: int, params: dict
) -> dict:
    """Выполнить сетевую рассылку по сегменту через broadcaster."""
    from collections import defaultdict
    from services import broadcaster

    text: str = str(params.get("text") or "").strip()
    segment: str = str(params.get("segment") or "all_each")
    lang: str = str(params.get("lang") or "")
    selected_bot_ids: list[int] = [int(x) for x in (params.get("selected_bot_ids") or [])]
    cluster_name: str = str(params.get("cluster_name") or "")
    _bc_buttons = params.get("buttons") or None

    if not text:
        return {"status": "failed", "summary": "⚠️ Текст рассылки не указан"}

    bots_all = await db.get_bots(pool, owner_id)
    if not bots_all:
        return {"status": "failed", "summary": "⚠️ Нет ботов для рассылки"}

    # Apply segment filter to bot list
    if segment == "selected_bots" and selected_bot_ids:
        bots = [b for b in bots_all if b["bot_id"] in set(selected_bot_ids)]
    elif segment == "cluster" and cluster_name:
        cluster_bot_rows = await _safe_fetch(
                pool,
            "SELECT bot_id FROM managed_bots WHERE added_by=$1 AND cluster=$2 AND is_active=TRUE",
            owner_id, cluster_name,
        )
        cluster_ids = {r["bot_id"] for r in cluster_bot_rows}
        bots = [b for b in bots_all if b["bot_id"] in cluster_ids]
    else:
        bots = list(bots_all)

    if not bots:
        return {"status": "failed", "summary": "⚠️ Нет ботов в выбранном сегменте"}

    total_started = 0
    total_users = 0
    launched_bc_ids: list[int] = []
    _BOT_START_DELAY_S = 2.0

    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", len(bots), op_id
    )

    # Pass None for session — broadcaster.run() creates its own session per task
    # (avoids closed-session bug when ClientSession exits before background tasks start)
    if segment in ("all_each", "selected_bots", "cluster"):
        for b in bots:
            if await _is_cancelled(pool, op_id):
                for _bc in launched_bc_ids:
                    broadcaster.cancel(_bc)
                return {
                    "status": "cancelled",
                    "ok": total_started,
                    "summary": f"Отменено. Запущено {total_started} из {len(bots)} ботов",
                }
            try:
                rows = await pool.fetch(
                    "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", b["bot_id"]
                )
            except Exception:
                log.warning("network_broadcast op=%d: fetch users failed bot=%s", op_id, b.get("bot_id"), exc_info=True)
                rows = []
            ids = [r["user_id"] for r in rows]
            if not ids:
                continue
            bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id, buttons=_bc_buttons)
            if not bc_id:
                continue
            broadcaster.start(
                pool, None, bc_id, b["token"], b["bot_id"], text, None, ids, _bc_buttons,
                start_delay=total_started * _BOT_START_DELAY_S,
            )
            launched_bc_ids.append(bc_id)
            total_started += 1
            total_users += len(ids)
            # Progress unit is "bots launched" (total_items=len(bots)), NOT users —
            # incrementing by len(ids) here overflowed done_items past total_items.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )

    elif segment == "unique":
        users = await db.get_unique_network_users(pool, owner_id)
        by_bot: dict = defaultdict(list)
        token_map: dict = {}
        for u in users:
            by_bot[u["bot_id"]].append(u["user_id"])
            token_map[u["bot_id"]] = u["token"]
        for bid, ids in by_bot.items():
            if await _is_cancelled(pool, op_id):
                for _bc in launched_bc_ids:
                    broadcaster.cancel(_bc)
                return {
                    "status": "cancelled",
                    "ok": total_started,
                    "summary": f"Отменено. Запущено {total_started} ботов",
                }
            bc_id = await db.create_broadcast(pool, bid, text, len(ids), owner_id, buttons=_bc_buttons)
            if not bc_id:
                continue
            broadcaster.start(
                pool, None, bc_id, token_map[bid], bid, text, None, ids, _bc_buttons,
                start_delay=total_started * _BOT_START_DELAY_S,
            )
            launched_bc_ids.append(bc_id)
            total_started += 1
            total_users += len(ids)
            # Progress unit is "bots launched" (total_items=len(bots)), NOT users —
            # incrementing by len(ids) here overflowed done_items past total_items.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )

    elif segment in ("cold_all", "lost_all"):
        days_from = 30 if segment == "lost_all" else 7
        days_to = None if segment == "lost_all" else 30
        for b in bots:
            if await _is_cancelled(pool, op_id):
                for _bc in launched_bc_ids:
                    broadcaster.cancel(_bc)
                return {
                    "status": "cancelled",
                    "ok": total_started,
                    "summary": f"Отменено. Запущено {total_started} из {len(bots)} ботов",
                }
            ids = await db.get_inactive_user_ids(pool, b["bot_id"], days_from, days_to)
            if not ids:
                continue
            bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id, buttons=_bc_buttons)
            if not bc_id:
                continue
            broadcaster.start(
                pool, None, bc_id, b["token"], b["bot_id"], text, None, ids, _bc_buttons,
                start_delay=total_started * _BOT_START_DELAY_S,
            )
            launched_bc_ids.append(bc_id)
            total_started += 1
            total_users += len(ids)
            # Progress unit is "bots launched" (total_items=len(bots)), NOT users —
            # incrementing by len(ids) here overflowed done_items past total_items.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )

    elif segment == "lang":
        for b in bots:
            if await _is_cancelled(pool, op_id):
                for _bc in launched_bc_ids:
                    broadcaster.cancel(_bc)
                return {
                    "status": "cancelled",
                    "ok": total_started,
                    "summary": f"Отменено. Запущено {total_started} из {len(bots)} ботов",
                }
            try:
                rows = await pool.fetch(
                    "SELECT user_id FROM bot_users WHERE bot_id=$1 AND language_code=$2 AND is_active=TRUE",
                    b["bot_id"], lang,
                )
            except Exception:
                log.warning("network_broadcast op=%d: fetch lang users failed bot=%s", op_id, b.get("bot_id"), exc_info=True)
                rows = []
            ids = [r["user_id"] for r in rows]
            if not ids:
                continue
            bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id, buttons=_bc_buttons)
            if not bc_id:
                continue
            broadcaster.start(
                pool, None, bc_id, b["token"], b["bot_id"], text, None, ids, _bc_buttons,
                start_delay=total_started * _BOT_START_DELAY_S,
            )
            launched_bc_ids.append(bc_id)
            total_started += 1
            total_users += len(ids)
            # Progress unit is "bots launched" (total_items=len(bots)), NOT users —
            # incrementing by len(ids) here overflowed done_items past total_items.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                op_id,
            )

    if total_started == 0:
        return {
            "status": "done",
            "ok": 0,
            "summary": "⚠️ Нет пользователей в выбранном сегменте — рассылка не запущена",
        }

    # Normalise progress counters to the bot-launch unit. total_items was set to
    # len(bots) up front, but bots without an audience are skipped, so done_items
    # (incremented per launched bot) could stay below total_items and read <100%.
    # Per-user delivery progress is tracked separately in the broadcasts table.
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1, done_items=$1 WHERE id=$2",
        total_started, op_id,
    )

    segment_labels = {
        "all_each": "Все боты → своей аудитории",
        "unique": "Уникальные пользователи",
        "cold_all": "Холодные (7–30 дн)",
        "lost_all": "Потерянные (30+ дн)",
        "lang": f"По языку: {lang}",
        "selected_bots": f"Выбранные боты ({total_started})",
        "cluster": f"Кластер «{cluster_name}» ({total_started} бот(ов))",
    }
    label = segment_labels.get(segment, segment)
    return {
        "status": "done",
        "ok": total_users,
        "bots_started": total_started,
        "summary": (
            f"📢 Сетевая рассылка запущена\n"
            f"Сегмент: {label}\n"
            f"Ботов: {total_started}, получателей: {total_users:,}"
        ),
    }


async def _exec_seed_presence_pack(
    pool: asyncpg.Pool, bot: "Bot", op_id: int, owner_id: int, params: dict
) -> dict:
    """Опубликовать начальные посты во всех каналах Presence Pack.

    params: {"pack_id": int}
    """
    import aiohttp as _aiohttp
    import json as _json
    from services import presence_setup as _ps

    pack_id: int = int(params.get("pack_id") or 0)
    if not pack_id:
        return {"status": "failed", "summary": "⚠️ pack_id не указан"}

    pack = await db.get_presence_pack(pool, pack_id, owner_id)
    if not pack:
        return {"status": "failed", "summary": f"⚠️ Presence Pack #{pack_id} не найден"}

    def _jlist(val) -> list:
        if isinstance(val, list):
            return val
        if val is None:
            return []
        try:
            return _json.loads(val) or []
        except Exception as e:
            log.warning('pack channel_ids parse failed: %s', e)
            return []

    ch_ids: list[int] = _jlist(pack["channel_ids"])
    if not ch_ids:
        return {"status": "done", "summary": "⚠️ В пакете нет каналов — посев пропущен"}

    # Resolve bot token if bot is linked
    bot_token: str | None = None
    if pack.get("bot_id"):
        try:
            from database.db import fetchrow_bot as _fetchrow_bot_sp
            bot_row = await _fetchrow_bot_sp(
                pool,
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                pack["bot_id"], owner_id,
            )
            if bot_row:
                bot_token = bot_row["token"] or ""
        except Exception:
            log.warning("_exec_seed_presence_pack op=%d: failed to fetch bot token", op_id)

    # Resolve a group link for cross-linking in seed post
    gr_ids: list[int] = _jlist(pack["group_ids"])
    group_link: str | None = None
    if gr_ids:
        try:
            gr_row = await pool.fetchrow(
                "SELECT username FROM managed_channels "
                "WHERE id = ANY($1::int[]) AND username IS NOT NULL LIMIT 1",
                gr_ids,
            )
            if gr_row:
                group_link = f"@{gr_row['username']}"
        except Exception:
            log.warning("_exec_seed_presence_pack op=%d: failed to fetch group link", op_id)

    try:
        channels = await pool.fetch(
            "SELECT title, username, channel_id, access_hash FROM managed_channels "
            "WHERE id = ANY($1::int[])",
            ch_ids,
        )
    except Exception as exc:
        log.error("_exec_seed_presence_pack op=%d: fetch channels failed: %s", op_id, exc)
        return {"status": "failed", "summary": "❌ Ошибка при загрузке каналов из БД"}

    missing_count = len(ch_ids) - len(channels)
    if missing_count > 0:
        log.warning(
            "_exec_seed_presence_pack op=%d: %d/%d channels not found in managed_channels "
            "(possibly deleted after pack was created)",
            op_id, missing_count, len(ch_ids),
        )

    success = 0
    fail = 0
    fail_names: list[str] = []
    total = len(ch_ids)  # use original count for accurate progress reporting
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
    )

    async with _aiohttp.ClientSession() as http:
        for idx, ch in enumerate(channels, 1):
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "summary": f"Отменено на {idx - 1}/{total}"}
            post_text = _ps.build_seed_post(
                channel_title=ch["title"] or ch.get("username") or pack["name"],
                bot_username=pack.get("bot_username"),
                group_link=group_link,
                target_url=pack.get("target_url"),
                target_label=pack.get("target_label"),
                pack_description=pack.get("description"),
            )
            chan_name = ch.get("title") or (
                f"@{ch['username']}" if ch.get("username") else f"id{ch['channel_id']}"
            )
            # Prefer @username for both methods — avoids access_hash requirement
            chan_username = f"@{ch['username']}" if ch.get("username") else None
            posted = False
            if bot_token:
                chan_target = chan_username or int(f"-100{ch['channel_id']}")
                posted = await _ps.seed_channel_post(http, bot_token, chan_target, post_text)
            if not posted:
                # For account method: use @username if available (no access_hash needed),
                # else fall back to numeric ID + access_hash
                acc_target: int | str = chan_username or ch["channel_id"]
                acc_hash = 0 if chan_username else (ch.get("access_hash") or 0)
                posted = await _ps.seed_channel_via_account(
                    pool, owner_id, acc_target, acc_hash, post_text
                )
            if posted:
                success += 1
            else:
                fail += 1
                fail_names.append(chan_name)

            # Update progress in operation_queue (offset by missing so bar is accurate)
            try:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                    missing_count + idx, op_id,
                )
            except Exception as e:
                log.warning("seed_presence_pack progress update failed for op %d: %s", op_id, e)

            await asyncio.sleep(2)

    if success > 0:
        try:
            await db.mark_presence_pack_seeded(pool, pack_id, owner_id)
        except Exception:
            log.warning(
                "_exec_seed_presence_pack op=%d: mark_presence_pack_seeded failed", op_id
            )

    missing_hint = f"\n⚠️ Не найдено в БД: {missing_count} канал(ов) — возможно удалены" if missing_count else ""
    fail_hint = ""
    if fail_names:
        names = ", ".join(fail_names[:3])
        extra = f" (+{len(fail_names) - 3})" if len(fail_names) > 3 else ""
        fail_hint = f"\n❌ Не удалось: {names}{extra}"

    status = "done" if success > 0 or (len(channels) == 0 and missing_count == total) else "done"
    return {
        "status": status,
        "ok": success,
        "fail": fail,
        "total": total,
        "summary": (
            f"🌱 Посев постов Presence Pack #{pack_id}\n"
            f"✅ Опубликовано: {success}/{len(channels)} найденных{missing_hint}{fail_hint}"
        ),
    }


async def _exec_promote_presence_pack(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Назначить бота администратором во всех каналах/группах Presence Pack."""
    from services import presence_setup as _ps
    from database import db as _db

    pack_id = int(params.get("pack_id", 0))
    bot_tg_id = int(params.get("bot_tg_id", 0))
    channel_ids: list[int] = [int(x) for x in (params.get("channel_ids") or [])]

    if not pack_id or not bot_tg_id or not channel_ids:
        return {"status": "failed", "summary": "⚠️ Неверные параметры promote_presence_pack"}

    total = len(channel_ids)
    success = 0
    fail = 0

    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
    )

    for idx, ch_id in enumerate(channel_ids, 1):
        if await _is_cancelled(pool, op_id):
            return {"status": "cancelled", "summary": f"Отменено на {idx - 1}/{total}"}
        try:
            row = await pool.fetchrow(
                "SELECT channel_id, access_hash FROM managed_channels WHERE id=$1", ch_id
            )
            if not row:
                fail += 1
                continue
            ok = await _ps.promote_bot_in_channel(
                pool, owner_id, row["channel_id"], row.get("access_hash") or 0, bot_tg_id
            )
            if ok:
                success += 1
            else:
                fail += 1
        except Exception as exc:
            log.warning("_exec_promote_presence_pack op=%d ch=%d: %s", op_id, ch_id, exc)
            fail += 1

        if idx % 3 == 0 or idx == total:
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=$1 WHERE id=$2", success + fail, op_id
            )
        await asyncio.sleep(2)

    if success > 0:
        try:
            await _db.mark_presence_pack_promoted(pool, pack_id, owner_id)
        except Exception:
            log.warning("_exec_promote_presence_pack op=%d: mark_promoted failed", op_id)

    fail_hint = f"\n⚠️ Ошибок: {fail}" if fail else ""
    return {
        "status": "done",
        "ok": success,
        "fail": fail,
        "total": total,
        "summary": (
            f"👑 Назначение бота admin — Presence Pack #{pack_id}\n"
            f"✅ Успешно: {success}/{total}{fail_hint}"
        ),
    }


async def _exec_bulk_seo_apply(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое применение AI SEO-предложений по всей сетке каналов.

    Переиспользует ЕДИНУЮ реализацию services.seo_apply.apply_seo_to_channel (та
    же, что инлайн-эндпоинт) — без дублирования. Изоляция сбоев по каналу,
    per-channel лог в operation_log, поддержка отмены.
    """
    from services.seo_apply import apply_seo_to_channel

    channel_ids = [int(x) for x in (params.get("channel_ids") or [])]
    if not channel_ids:
        return {"status": "failed", "summary": "⚠️ Нет каналов для применения SEO"}

    total = len(channel_ids)
    await _safe_execute(pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    ok_count, fail_count = 0, 0
    for idx, chan_id in enumerate(channel_ids, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            res = await apply_seo_to_channel(pool, owner_id, chan_id)
        except Exception as exc:
            log.warning("bulk_seo_apply op=%d chan=%s: %s", op_id, chan_id, exc)
            res = {"ok": False, "error": str(exc)[:200]}
        if res.get("ok"):
            ok_count += 1
            applied = ", ".join((res.get("applied") or {}).keys()) or "без изменений"
            await _safe_execute(pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'ok',$4)",
                op_id, idx, f"ch#{chan_id}", ("SEO: " + applied)[:200])
        else:
            fail_count += 1
            await _safe_execute(pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'error',$4)",
                op_id, idx, f"ch#{chan_id}", (res.get("error") or "не удалось")[:200])
        await _safe_execute(pool,
            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            await asyncio.sleep(2.0)

    summary = (f"🔍 SEO по сетке: применено {ok_count}/{total}"
               + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else ""))
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


async def _exec_bulk_edit_channels(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое редактирование title/about каналов всех указанных аккаунтов."""
    from services import account_manager

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    field = params.get("field", "title")
    value = params.get("value", "")

    if not account_ids or not value:
        return {"status": "failed", "reason": "Не указаны аккаунты или значение поля"}

    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    # Отказной захват: массовое редактирование открывает живую сессию на каждом
    # аккаунте через account_manager. Без захвата аккаунт мог одновременно вести
    # другую операцию → две сессии на одном auth-key → AUTH_KEY_DUPLICATED.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:
        ok_total = 0
        err_total = 0
        step = 0
        await _safe_execute(
                pool,
            "UPDATE operation_queue SET total_items=$1 WHERE id=$2", len(accounts), op_id
        )

        for acc in accounts:
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_total,
                    "fail": err_total,
                    "summary": f"Отменено. Изменено: {ok_total}",
                }
            try:
                dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
            except Exception as exc:
                log.warning("_exec_bulk_edit_channels get_dialogs acc=%s: %s", acc.get("id"), exc)
                err_total += 1
                await _safe_execute(
                        pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
                continue

            channels = [d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")]
            for ch in channels:
                ch_id = ch["id"]
                step += 1
                try:
                    if field == "title":
                        ok = await account_manager.edit_channel_title(acc["session_str"], ch_id, value, _acc=acc)
                    else:
                        ok = await account_manager.edit_channel_about(acc["session_str"], ch_id, value, _acc=acc)
                    if ok:
                        ok_total += 1
                    else:
                        err_total += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_exc_swallow(log, "bulk_edit_channels ch=%s: %s", ch_id, exc)
                    err_total += 1
                await asyncio.sleep(2)

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

        return {
            "status": "done",
            "ok": ok_total,
            "fail": err_total,
            "summary": (f"✏️ Редактирование каналов ({field}): ✅ {ok_total} ❌ {err_total}"
                        + (f" · пропущено занятых аккаунтов: {_busy}" if _busy else "")),
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_group_import_all(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Импорт групп со всех аккаунтов пользователя в managed_channels."""
    from services import account_manager
    from database.db import add_managed_channels

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    if account_ids:
        rows = await resource_selector.select_all_active(
            pool, owner_id, include_ids=account_ids, min_trust_score=0.0)
    else:
        rows = await resource_selector.select_all_active(
            pool, owner_id, min_trust_score=0.0)
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    # Отказной захват: каждый аккаунт работает своей живой сессией. Без захвата
    # он мог параллельно вести другую операцию → две сессии на одном auth-key.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        total_imported = 0
        total_foreign = 0  # группы, где аккаунт лишь участник — чужие, не тянем
        errors: list[str] = []
        n = len(accounts)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", n, op_id)

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "imported": total_imported,
                    "summary": f"Отменено. Импортировано: {total_imported}",
                }
            try:
                dialogs = await account_manager.get_dialogs(acc["session_str"], limit=200, _acc=acc) or []
                typed = [
                    d for d in dialogs
                    if d.get("type") in ("megagroup", "supergroup", "group", "chat", "gigagroup")
                ]
                # «Моя инфраструктура» = группы, где аккаунт создатель или админ.
                groups = [d for d in typed if d.get("is_admin") or d.get("is_creator")]
                total_foreign += len(typed) - len(groups)
                if groups:
                    # add_managed_channels — НЕ upsert_managed_channels(): get_dialogs(limit=200)
                    # отдаёт максимум 200 ДИАЛОГОВ (не 200 групп), поэтому groups — частичный
                    # срез при >200 диалогов у аккаунта. upsert_managed_channels() удалила бы
                    # ВСЕ ранее сохранённые каналы/группы аккаунта перед вставкой этого среза.
                    await add_managed_channels(pool, owner_id, acc["id"], groups)
                    total_imported += len(groups)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("_exec_group_import_all acc=%s: %s", acc.get("id"), exc)
                acc_label = acc.get("first_name") or acc.get("phone") or str(acc["id"])
                errors.append(f"• {acc_label}: {str(exc)[:60]}")

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < n - 1:
                await asyncio.sleep(2)

        err_hint = f"\n⚠️ Ошибок по аккаунтам: {len(errors)}" if errors else ""
        foreign_hint = f"\n🚫 Пропущено чужих (только участник): {total_foreign}" if total_foreign else ""
        return {
            "status": "done",
            "imported": total_imported,
            "foreign_skipped": total_foreign,
            "accounts": n,
            "summary": f"📥 Импорт групп: {total_imported} групп из {n} аккаунтов{foreign_hint}{err_hint}",
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_group_announce(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Рассылка объявления во все группы выбранного аккаунта."""
    from services import account_manager
    # Anti-detection: свой вариант текста на каждую группу (spintax; no-op без него).
    from services.dm_engine import expand_spintax as _expand_spintax

    acc_id = int(params.get("acc_id", 0))
    text = params.get("text", "")
    if not acc_id or not text:
        return {"status": "failed", "reason": "Не указан аккаунт или текст"}

    row = await _safe_fetchrow(
            pool,
        "SELECT id, session_str, first_name, phone, device_model, system_version, app_version, "
        "lang_code, system_lang_code, "
        "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE AND session_str IS NOT NULL",
        acc_id, owner_id,
    )
    if not row:
        return {"status": "failed", "reason": "Аккаунт не найден или неактивен"}
    acc = dict(row)

    # Отказной захват: объявление рассылается живой сессией этого аккаунта.
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "reason": "Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        # Риск-пульс: аккаунт под недавним серьёзным ограничением — рассылка объявлений
        # с него = быстрый бан. Останавливаем ради защиты (снимется автоматически, когда
        # ограничение устареет). fail-open: ошибка проверки не блокирует операцию.
        try:
            if await _infra_mem.is_account_quarantined(pool, acc["id"]):
                return {"status": "failed", "reason": "Аккаунт под риск-пульсом (недавнее "
                        "ограничение) — рассылка остановлена для защиты от бана. Повторите позже."}
        except Exception:
            log_exc_swallow(log, f"group_announce op={op_id}: quarantine check failed")

        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
        groups = [
            d for d in dialogs
            if d.get("type") in ("megagroup", "supergroup", "group", "chat")
        ]
        if not groups:
            return {"status": "done", "ok": 0, "fail": 0, "summary": "Нет групп у аккаунта"}

        total = len(groups)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

        ok_count = 0
        err_count = 0
        for idx, grp in enumerate(groups):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": err_count,
                    "summary": f"Отменено. Отправлено: {ok_count}/{total}",
                }
            access_hash = grp.get("access_hash", 0) or 0
            _ann = _expand_spintax(text)  # свой вариант объявления в эту группу
            try:
                result = await account_manager.post_to_channel(
                    acc["session_str"], grp["id"], _ann, access_hash=access_hash, _acc=acc
                )
                if "error" in result or result.get("banned"):
                    err_count += 1
                else:
                    ok_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exc_swallow(log, "group_announce: post_to_channel grp=%s: %s", grp.get("id"), exc)
                err_count += 1

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < total - 1:
                # межцелевой темп анонса в группы под губернатором
                await asyncio.sleep(await _governed_delay(pool, owner_id, 3.0))

        return {
            "status": "done",
            "ok": ok_count,
            "fail": err_count,
            "summary": f"📢 Объявление: ✅ {ok_count} ❌ {err_count} из {total} групп",
        }
    finally:
        await release_accounts([_claimed_acc])


async def _exec_bulk_dm_adhoc(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Рассылка личных сообщений по списку usernames с нескольких аккаунтов (round-robin)."""
    from services import account_manager
    from database import db as _db
    # Anti-detection: каждый получатель получает свой вариант текста (spintax);
    # без spintax expand возвращает текст как есть (no-op). Идентичные ЛС многим —
    # самая палевная сигнатура (PeerFlood/spam-репорт).
    from services.dm_engine import expand_spintax as _expand_spintax
    # Исходы отправки различаем общим классификатором: флуд-вейт, флаг PeerFlood,
    # мёртвая сессия и «получатель закрыл ЛС» требуют РАЗНОЙ реакции.
    from services.dm_engine import classify_send_result as _classify_send_result
    # Тот же отбор аккаунта по лимиту, что в кампаниях: чистая, отдельно
    # протестированная функция вместо второго кустарного round-robin.
    from services.dm_engine import pick_account_under_cap as _pick_acc

    # PeerFlood — флаг на аккаунте, а не на получателе: короткой паузы мало,
    # иначе следующая операция добьёт помеченный аккаунт (паритет с dm_engine).
    _PEER_FLOOD_COOLDOWN_S = 48 * 3600

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    usernames: list[str] = params.get("usernames") or []
    # Дубли в списке = два одинаковых ЛС одному человеку с разных аккаунтов —
    # самая заметная спам-сигнатура. Схлопываем, сохраняя порядок.
    if usernames:
        _seen_refs: set[str] = set()
        _uniq: list[str] = []
        for _u in usernames:
            _key = str(_u).lstrip("@").strip().lower()
            if not _key or _key in _seen_refs:
                continue
            _seen_refs.add(_key)
            _uniq.append(_u)
        usernames = _uniq
    text: str = params.get("text") or ""
    delay: float = float(params.get("delay") or 2.5)
    # Потолок сообщений с ОДНОГО аккаунта за эту рассылку. У кампаний дневной
    # лимит есть давно, у разовой рассылки не было ничего: 1000 получателей на
    # двух аккаунтах — это по 500 ЛС с каждого и почти верный бан.
    try:
        per_acc_cap = int(params.get("per_account_cap") or 0) or None
    except (TypeError, ValueError):
        per_acc_cap = None

    # Медиа (опц.): файл лежит на диске контейнера, читаем один раз. С медиа
    # текст становится ПОДПИСЬЮ и может быть пустым.
    media_path = params.get("media_path")
    media_filename = params.get("media_filename") or "media"
    media_bytes: bytes | None = None
    if media_path:
        try:
            import os as _os
            if _os.path.exists(media_path):
                with open(media_path, "rb") as _f:
                    media_bytes = _f.read()
        except Exception:
            log_exc_swallow(log, f"bulk_dm_adhoc op={op_id}: media read failed")

    if not account_ids or not usernames or (not text and media_bytes is None):
        return {"status": "failed", "reason": "Не указаны аккаунты, получатели или текст/медиа"}

    # Реестр «не писать»: раньше применялся только в масс-инвайте, из-за чего
    # человек, явно попросивший не писать, получал разовую рассылку. Fail-open —
    # сбой реестра не срывает операцию.
    _opt_out_skipped = 0
    try:
        from services import contact_opt_out as _coo
        from services.dm_engine import filter_opted_out_refs as _filter_refs

        _opted = await _coo.load_opted_out(pool, owner_id)
        if _opted:
            usernames, _opt_out_skipped = _filter_refs(usernames, _opted)
            if _opt_out_skipped:
                log.info("bulk_dm_adhoc op=%d: пропущено %d по реестру «не писать»",
                         op_id, _opt_out_skipped)
    except Exception:
        log_exc_swallow(log, f"bulk_dm_adhoc op={op_id}: opt-out filter failed")
    if not usernames:
        return {"status": "done", "ok": 0, "fail": 0,
                "summary": f"🚫 Все {_opt_out_skipped} получателей в реестре «не писать» — отправлять некому"}

    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    active_accounts = [dict(r) for r in rows]
    if not active_accounts:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    # Риск-пульс (fail-open): не шлём ЛС с аккаунтов под недавним серьёзным
    # ограничением — рассылка с флагнутого аккаунта = быстрый бан. Если все в
    # карантине — работаем всеми (лучше рискнуть, чем обнулить операцию).
    _skipped_quar = 0
    try:
        _kept = [a for a in active_accounts
                 if not await _infra_mem.is_account_quarantined(pool, a["id"])]
        if _kept and len(_kept) != len(active_accounts):
            _skipped_quar = len(active_accounts) - len(_kept)
            log.info("bulk_dm_adhoc op=%d: пропущено %d аккаунтов в карантине",
                     op_id, _skipped_quar)
            active_accounts = _kept
    except Exception:
        log_exc_swallow(log, f"bulk_dm_adhoc op={op_id}: quarantine check failed")

    # Отказной захват — ПОСЛЕ фильтра карантина: нет смысла занимать аккаунты,
    # которые тут же отбросим. Каждый работает своей живой сессией.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in active_accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(active_accounts) - len(claimed_ids)
    active_accounts = [a for a in active_accounts if int(a["id"]) in set(claimed_ids)]

    try:

        total = len(usernames)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

        ok_count = 0
        err_count = 0
        skip_count = 0     # получатель недостижим (приватность/блок/удалён) — не наша ошибка
        flood_wait_total = 0.0
        acc_idx = 0
        sent_by_acc: dict[int, int] = {}

        async def _log_step(step: int, target: str, status: str, message: str) -> None:
            """Построчный аудит в «📋 Лог» операции.

            Раньше разовая рассылка не писала НИ ОДНОЙ строки: пользователь
            видел только «✅ N ❌ M» и не мог узнать, кому именно не дошло.
            """
            await _safe_execute(
                pool,
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,$4,$5)",
                op_id, step, str(target)[:64], status, str(message)[:200],
            )

        for i, username in enumerate(usernames):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": err_count,
                    "summary": f"Отменено. Отправлено: {ok_count}/{total}",
                }

            if not active_accounts:
                err_count += 1
                await _log_step(i + 1, username, "error", "не осталось рабочих аккаунтов")
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            # Явный вращающийся индекс вместо i % len: при выбывании аккаунта
            # из ротации модуль по счётчику получателей перескакивал через
            # оставшиеся аккаунты и грузил одни сильнее других.
            acc, acc_idx = _pick_acc(active_accounts, acc_idx, sent_by_acc, per_acc_cap)
            if acc is None:
                # Все аккаунты выбрали лимит на эту рассылку — это защита, а не
                # ошибка: честно сообщаем и не дожимаем флот.
                _left = total - i
                skip_count += _left
                await _log_step(i + 1, username, "skip",
                                f"лимит {per_acc_cap} на аккаунт исчерпан")
                log.info("bulk_dm_adhoc op=%d: лимит на аккаунт исчерпан, не отправлено %d",
                         op_id, _left)
                break
            _msg = _expand_spintax(text)  # свой вариант текста этому получателю

            try:
                if media_bytes is not None:
                    # Медиа с подписью — уникализируем под каждого (анти-детект).
                    _sent = await account_manager.send_media_via_account(
                        acc["session_str"], username, caption=_msg, _acc=acc,
                        media_bytes=media_bytes, media_filename=media_filename,
                        uniquify=True)
                    result = {"ok": True} if _sent else {"error": "media send failed"}
                else:
                    result = await account_manager.send_dm(
                        acc["session_str"], username, _msg, _acc=acc
                    )

                kind = _classify_send_result(result)
                _err_text = str(result.get("error") or "")

                if kind == "sent":
                    ok_count += 1
                    sent_by_acc[int(acc["id"])] = sent_by_acc.get(int(acc["id"]), 0) + 1
                    await _log_step(i + 1, username, "ok", f"отправлено · акк {acc['id']}")
                elif kind == "flood":
                    # Флуд-вейт — ограничение КОНКРЕТНОГО аккаунта. Ставим cooldown
                    # и выводим его из ротации, иначе следующий получатель уйдёт с
                    # того же аккаунта и добьёт его.
                    fw = int(result.get("flood_wait") or 0)
                    flood_wait_total += fw
                    err_count += 1
                    await _safe_execute(
                        pool,
                        "UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'), "
                        "last_flood_at = NOW(), flood_count_7d = COALESCE(flood_count_7d, 0) + 1 "
                        "WHERE id=$2",
                        min(max(fw, 60), 3600), acc["id"],
                    )
                    if len(active_accounts) > 1:
                        active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
                    await _log_step(i + 1, username, "error", f"FloodWait {fw}с · акк {acc['id']}")
                    log.info("bulk_dm_adhoc: flood_wait %ss acc=%s → выведен из ротации", fw, acc["id"])
                elif kind == "peer_flood":
                    # Аккаунт помечен Telegram за спам ВООБЩЕ. Продолжать им —
                    # гарантированная потеря аккаунта: длинный кулдаун и из ротации.
                    err_count += 1
                    await _safe_execute(
                        pool,
                        "UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'), "
                        "last_flood_at = NOW(), flood_count_7d = COALESCE(flood_count_7d, 0) + 1 "
                        "WHERE id=$2",
                        _PEER_FLOOD_COOLDOWN_S, acc["id"],
                    )
                    active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
                    await _log_step(i + 1, username, "error", f"PeerFlood · акк {acc['id']} выведен")
                    log.warning("bulk_dm_adhoc: PeerFlood acc=%s — аккаунт выведен из рассылки", acc["id"])
                elif kind == "auth":
                    # Мёртвая сессия. Прежний код искал ключ 'banned', которого
                    # send_dm никогда не возвращает, — то есть не ловил это вообще.
                    await _db.deactivate_account(pool, acc["id"], "dead session in bulk_dm_adhoc")
                    await _on_account_banned(pool, owner_id, acc["id"], "bulk_dm_adhoc", bot=bot)
                    active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
                    err_count += 1
                    await _log_step(i + 1, username, "error", f"сессия мертва · акк {acc['id']}")
                    log.info("bulk_dm_adhoc: dead session acc %s, removed from pool", acc["id"])
                elif kind == "skip":
                    # Получатель недостижим навсегда (приватность/чёрный список/
                    # удалён). Аккаунт здоров — не трогаем его и не считаем это
                    # нашей ошибкой, иначе метрика «ошибок» врёт про здоровье флота.
                    skip_count += 1
                    await _log_step(i + 1, username, "skip", _err_text or "получатель недоступен")
                else:
                    err_count += 1
                    await _log_step(i + 1, username, "error", _err_text or "unknown")
                    log.warning("bulk_dm_adhoc: failed @%s: %s", username, _err_text or "unknown")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exc_swallow(log, "bulk_dm_adhoc: send_dm @%s: %s", username, exc)
                err_count += 1
                await _log_step(i + 1, username, "error", str(exc))

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            # adaptive delay: базовый темп под губернатором + накопленный flood-wait
            # (flood мандатный — не масштабируем).
            wait = await _governed_delay(pool, owner_id, delay) + min(flood_wait_total, 30.0)
            flood_wait_total = max(0.0, flood_wait_total - delay)
            if i < total - 1:
                await asyncio.sleep(wait)

        # Медиа-файл больше не нужен — удаляем (диск контейнера эфемерный, но не
        # копим мусор при многих отправках).
        if media_path:
            try:
                import os as _os
                _os.unlink(media_path)
            except Exception:
                pass

        _quar_note = f" · 🛡 {_skipped_quar} аккаунтов пропущено (риск-пульс)" if _skipped_quar else ""
        _oo_note = f" · 🚫 {_opt_out_skipped} в реестре «не писать»" if _opt_out_skipped else ""
        # «Недоступен» ≠ «ошибка»: закрытые ЛС и удалённые аккаунты получателей
        # смешивались с настоящими сбоями и создавали ложную картину проблем флота.
        _skip_note = f" · 🔒 {skip_count} недоступны (закрытые ЛС/удалены)" if skip_count else ""
        return {
            "status": "done",
            "ok": ok_count,
            "fail": err_count,
            "skipped": skip_count,
            "opt_out_skipped": _opt_out_skipped,
            "summary": (f"📨 Рассылка ЛС: ✅ {ok_count} ❌ {err_count} из {total} получателей"
                        f"{_skip_note}{_quar_note}{_oo_note}"),
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_pin_last_post(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Закрепить последний пост в канале от имени аккаунта-владельца/админа."""
    import html as _html
    from services import account_manager

    channel_ref = params.get("channel_ref")
    account_id = params.get("account_id")
    access_hash = int(params.get("access_hash", 0) or 0)
    if not channel_ref or not account_id:
        return {"status": "failed", "reason": "Не указан channel_ref или account_id"}

    row = await _safe_fetchrow(
        pool,
        "SELECT id, session_str, first_name, phone, device_model, system_version, "
        "app_version, lang_code, system_lang_code, "
        "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id "
        "AND up.is_active=TRUE) AS proxy_url "
        "FROM tg_accounts "
        "WHERE owner_id=$1 AND id=$2 AND is_active=TRUE AND session_str IS NOT NULL",
        owner_id, int(account_id),
    )
    if not row:
        return {"status": "failed", "reason": "Аккаунт не найден или неактивен"}
    acc = dict(row)

    # Отказной захват: операция работает живой сессией этого аккаунта.
    # Без захвата он мог параллельно вести другую операцию (две сессии на
    # одном auth-key → AUTH_KEY_DUPLICATED).
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        await _safe_execute(pool, "UPDATE operation_queue SET total_items=1 WHERE id=$1", op_id)

        try:
            result = await account_manager.pin_last_channel_post(
                acc["session_str"], channel_ref, access_hash=access_hash, _acc=acc)
        except asyncio.CancelledError:
            raise
        except Exception as _pin_exc:
            log.warning("_exec_pin_last_post acc=%s: %s", acc.get("id"), _pin_exc)
            result = {"error": str(_pin_exc)[:120]}

        await _safe_execute(pool, "UPDATE operation_queue SET done_items=1 WHERE id=$1", op_id)

        if "pinned_msg_id" in result:
            return {
                "status": "done", "ok": 1, "failed": 0,
                "summary": f"\U0001F4CC Закреплён пост msg_id={result['pinned_msg_id']}",
            }
        return {
            "status": "failed", "ok": 0, "failed": 1,
            "reason": result.get("error", "ошибка"),
            "summary": f"❌ {_html.escape(str(result.get('error', 'ошибка'))[:120])}",
        }
    finally:
        await release_accounts([_claimed_acc])


async def _exec_bulk_post_to_channel(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Публикация текста в канал от нескольких аккаунтов."""
    import html as _html
    from services import account_manager
    from database import db as _db
    from bot.utils.op_helpers import backoff, _progress_text
    # Anti-detection: разные аккаунты в ОДИН канал одинаковым текстом — самая явная
    # сигнатура координации. Каждый аккаунт постит свой вариант (spintax; no-op без него).
    from services.dm_engine import expand_spintax as _expand_spintax

    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    channel_ref = params.get("channel_ref", "")
    text_to_post = params.get("text_to_post", "")
    bulk_access_hash = int(params.get("bulk_access_hash", 0) or 0)
    chat_id = params.get("chat_id")
    message_id = params.get("message_id")

    if not account_ids or not channel_ref or not text_to_post:
        return {"status": "failed", "reason": "Не указан channel_ref, text_to_post или account_ids"}

    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Аккаунты не найдены или неактивны"}

    total = len(accounts)
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    # Риск-пульс (fail-open): не постим с аккаунтов под недавним серьёзным
    # ограничением. Разные аккаунты в один канал + флагнутый = быстрый бан.
    # Все в карантине → работаем всеми (лучше рискнуть, чем обнулить).
    _skipped_quar = 0
    try:
        _kept = [a for a in accounts
                 if not await _infra_mem.is_account_quarantined(pool, a["id"])]
        if _kept and len(_kept) != len(accounts):
            _skipped_quar = len(accounts) - len(_kept)
            log.info("bulk_post_to_channel op=%d: пропущено %d аккаунтов в карантине",
                     op_id, _skipped_quar)
            accounts = _kept
            total = len(accounts)
            await _safe_execute(
                    pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)
    except Exception:
        log_exc_swallow(log, f"bulk_post_to_channel op={op_id}: quarantine check failed")

    # Отказной захват — ПОСЛЕ фильтра карантина: нет смысла занимать аккаунты,
    # которые тут же отбросим. Каждый работает своей живой сессией.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        ok_list: list[str] = []
        err_list: list[str] = []
        attempt = 0

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": len(ok_list),
                    "failed": len(err_list),
                    "summary": f"Отменено. Опубликовано: {len(ok_list)}, ошибок: {len(err_list)}",
                }

            label = _html.escape(acc.get("first_name") or acc.get("phone") or str(acc["id"]))
            _body = _expand_spintax(text_to_post)  # свой вариант текста от этого аккаунта
            try:
                result = await account_manager.post_to_channel(
                    acc["session_str"],
                    channel_ref,
                    _body,
                    access_hash=bulk_access_hash,
                    _acc=acc,
                )
            except asyncio.CancelledError:
                raise
            except Exception as _post_exc:
                log.warning("_exec_bulk_post_to_channel acc=%s: %s", acc.get("id"), _post_exc)
                err_list.append(f"❌ {label}: {_html.escape(str(_post_exc)[:60])}")
                await _safe_execute(
                        pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
                continue
            if result.get("banned"):
                await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                await _on_account_banned(pool, owner_id, acc["id"], "bulk_op", bot=bot)
                err_list.append(f"❌ {label}: забанен")
            elif result.get("flood_wait"):
                err_list.append(f"⏳ {label}: flood_wait, пропущен")
            elif "msg_id" in result:
                ok_list.append(f"✅ {label}: msg_id={result['msg_id']}")
                # Сигнал активности канала (last_post_at) — когда известен numeric id.
                if chat_id:
                    try:
                        await pool.execute(
                            "UPDATE managed_channels SET last_post_at=now() "
                            "WHERE owner_id=$1 AND channel_id=$2",
                            owner_id, int(chat_id),
                        )
                    except Exception:
                        log_exc_swallow(log, "bulk_post: last_post_at update")
            else:
                err_str = result.get("error", "ошибка")
                if _is_dead_session_error(err_str):
                    try:
                        await pool.execute(
                            "UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired',"
                            " status_reason=$2 WHERE id=$1 AND is_active=TRUE",
                            acc["id"], f"Dead session (bulk_post): {err_str[:160]}",
                        )
                    except Exception as e:
                        log_exc_swallow(log, f"bulk_post: dead session deactivate failed for acc {acc['id']}: {e}")
                err_list.append(f"❌ {label}: {_html.escape(err_str[:60])}")

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

            if chat_id and message_id:
                try:
                    await bot.edit_message_text(
                        _progress_text(
                            "Публикую посты...",
                            idx + 1, total, len(ok_list), len(err_list),
                        ),
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log_exc_swallow(log, f"bulk_post progress message edit failed: {e}")

            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            # Пауза из ответа Telegram — через общую обёртку с пределом.
            # max(backoff, flood) выглядел ограниченным, но backoff ограничивает
            # только СВОЁ слагаемое: flood_wait в час проходил через max() целиком
            # и останавливал прогон на час вместе со слотом и всем флотом.
            slept = await bounded_flood_sleep(
                result.get("flood_wait", 0) or 0, "bulk_post")
            await asyncio.sleep(max(0.0, backoff(attempt) - slept))

        lines = (
            [f"\U0001f4e4 <b>Публикация в {_html.escape(channel_ref)}</b>\n"]
            + ok_list
            + err_list
        )
        if _skipped_quar:
            lines.append(f"🛡 {_skipped_quar} аккаунтов пропущено (риск-пульс, защита от бана)")
        final_text = "\n".join(lines)

        if chat_id and message_id:
            try:
                from aiogram.utils.keyboard import InlineKeyboardBuilder
                from bot.callbacks import BmCb

                kb = InlineKeyboardBuilder()
                kb.button(
                    text="\U0001f4cb Детали операции",
                    callback_data=BmCb(action="op_detail", op_id=op_id),
                )
                await bot.edit_message_text(
                    final_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=kb.as_markup(),
                )
            except Exception as e:
                log_exc_swallow(log, f"bulk_post final message edit failed: {e}")

        return {
            "status": "done",
            "ok": len(ok_list),
            "failed": len(err_list),
            "summary": final_text[:500],
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_bulk_update_profile(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Обновление поля профиля на нескольких аккаунтах."""
    import html as _html
    from services import account_manager
    from database import db as _db
    from bot.utils.op_helpers import backoff, _progress_text

    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    field = params.get("field", "")
    value = params.get("value", "")
    chat_id = params.get("chat_id")
    message_id = params.get("message_id")

    if not account_ids or not field or value == "":
        return {"status": "failed", "reason": "Не указано field, value или account_ids"}

    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Аккаунты не найдены или неактивны"}

    # Отказной захват: каждый аккаунт работает своей живой сессией.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        total = len(accounts)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

        ok_list: list[str] = []
        err_list: list[str] = []
        attempt = 0

        for i, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": len(ok_list),
                    "failed": len(err_list),
                    "summary": f"Отменено. Обновлено: {len(ok_list)}, ошибок: {len(err_list)}",
                }

            label = _html.escape(acc.get("first_name") or acc.get("phone") or str(acc["id"]))
            actual_value = f"{value}{i + 1}" if field == "username" else value

            try:
                if field == "username":
                    result = await account_manager.update_account_username(
                        acc["session_str"], actual_value, _acc=acc
                    )
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result and not isinstance(result, dict):
                        err_list.append(f"❌ {label}: {_html.escape(str(result)[:50])}")
                    else:
                        ok_list.append(f"✅ {label}: @{_html.escape(actual_value)}")
                else:
                    result = await account_manager.update_profile(
                        acc["session_str"], **{field: value}, _acc=acc
                    )
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result:
                        ok_list.append(f"✅ {label}")
                    else:
                        err_list.append(f"❌ {label}: ошибка")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err_list.append(f"❌ {label}: {str(e)[:50]}")

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

            if chat_id and message_id:
                try:
                    await bot.edit_message_text(
                        _progress_text(
                            "Обновляю профили...",
                            i + 1, total, len(ok_list), len(err_list),
                        ),
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log_exc_swallow(log, f"bulk_update_profile progress message edit failed: {e}")

            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            await asyncio.sleep(backoff(attempt, base=2.0, cap=30.0))

        lines = [f"✏️ <b>Обновление {field}</b>\n"] + ok_list + err_list
        final_text = "\n".join(lines)

        if chat_id and message_id:
            try:
                from aiogram.utils.keyboard import InlineKeyboardBuilder
                from bot.callbacks import BmCb

                kb = InlineKeyboardBuilder()
                kb.button(
                    text="\U0001f4cb Детали операции",
                    callback_data=BmCb(action="op_detail", op_id=op_id),
                )
                await bot.edit_message_text(
                    final_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=kb.as_markup(),
                )
            except Exception as e:
                log_exc_swallow(log, f"bulk_update_profile final message edit failed: {e}")

        return {
            "status": "done",
            "ok": len(ok_list),
            "failed": len(err_list),
            "summary": final_text[:500],
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_bulk_chan_exec(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Bulk set username or about for channels across multiple accounts."""
    import html as _html
    from services import account_manager

    channel_acc_pairs: list[dict] = params.get("channel_acc_pairs") or []
    op: str = params.get("op", "")
    base_uname: str = params.get("base_uname", "")
    value: str = params.get("value", "")

    # Brand injection for free-tier users editing channel descriptions
    if op == "chan_about":
        try:
            from services import brand_injection as _bi
            if await _bi.is_user_free_tier(pool, owner_id):
                value = _bi.add_promo_to_description(value)
        except Exception as e:
            log_exc_swallow(log, f"bulk_chan_exec brand_injection failed: {e}")

    if not channel_acc_pairs or op not in ("chan_uname", "chan_about", "chan_title"):
        return {"status": "failed", "reason": "Не указаны channel_acc_pairs или неверный op"}

    # Collect unique acc_ids and fetch sessions from DB (never pass session_str in params)
    acc_ids = list({int(p["acc_id"]) for p in channel_acc_pairs})
    rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in acc_ids], min_trust_score=0.0)
    if not rows:
        return {"status": "failed", "reason": "Нет активных аккаунтов"}

    acc_map = {int(r["id"]): dict(r) for r in rows}

    # Отказной захват вместо безусловной пометки. Пары «канал↔аккаунт» с
    # незахваченным аккаунтом выбрасываем: работать ими нельзя, а молча оставить
    # их в total — соврать в прогрессе и в итоге.
    claimed_ids = await try_claim_accounts([int(a) for a in acc_map])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _claimed_set = set(claimed_ids)
    _pairs_before = len(channel_acc_pairs)
    channel_acc_pairs = [p for p in channel_acc_pairs
                         if int(p["acc_id"]) in _claimed_set]
    _busy = _pairs_before - len(channel_acc_pairs)

    total = len(channel_acc_pairs)
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    ok_list: list[str] = []
    err_list: list[str] = []

    try:
        for idx, pair in enumerate(channel_acc_pairs):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": len(ok_list),
                    "fail": len(err_list),
                    "summary": f"Отменено. Изменено: {len(ok_list)}",
                }

            ch_id = pair["channel_id"]
            acc_id = int(pair["acc_id"])
            chan_title = _html.escape(str(pair.get("title") or ch_id))
            # access_hash/username нужны, чтобы свежий клиент нашёл канал по id
            # (иначе смена названия/описания давала 0 — get_entity(id) падал).
            _pair_hash = int(pair.get("access_hash") or 0)
            _pair_uname = str(pair.get("username") or "")
            acc = acc_map.get(acc_id)
            if not acc:
                err_list.append(f"❌ {chan_title}: аккаунт не найден")
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            try:
                if op == "chan_uname":
                    from services.username_engine import (
                        unique_channel_username,
                        generate_username_variants,
                        _short_suffix,
                    )

                    initial = unique_channel_username(base_uname, idx)
                    variants_to_try = [initial]
                    for v in generate_username_variants(f"{base_uname}{_short_suffix(idx, 2)}"):
                        if v not in variants_to_try:
                            variants_to_try.append(v)

                    assigned = None
                    last_err = ""
                    for variant in variants_to_try[:12]:
                        err = await account_manager.set_channel_username(
                            acc["session_str"], ch_id, variant, _acc=acc
                        )
                        if not err:
                            assigned = variant
                            break
                        last_err = err
                        if not any(
                            k in err.lower()
                            for k in ("taken", "occupied", "username_occupied", "занят", "already")
                        ):
                            break
                        await asyncio.sleep(2.0)

                    if assigned:
                        ok_list.append(f"✅ {chan_title}: @{assigned}")
                        try:
                            await pool.execute(
                                "UPDATE managed_channels SET username=$1 WHERE owner_id=$2 AND channel_id=$3",
                                assigned, owner_id, ch_id,
                            )
                        except Exception as e:
                            log_exc_swallow(log, f"bulk_chan_exec: persist username failed for ch={ch_id}: {e}")
                    else:
                        err_list.append(f"❌ {chan_title}: {_html.escape(last_err[:60])}")

                elif op == "chan_about":
                    ok = await account_manager.edit_channel_about(
                        acc["session_str"], ch_id, value, _acc=acc,
                        access_hash=_pair_hash, username=_pair_uname,
                    )
                    if ok:
                        ok_list.append(f"✅ {chan_title}")
                    else:
                        err_list.append(f"❌ {chan_title}: ошибка обновления")

                elif op == "chan_title":
                    ok = await account_manager.edit_channel_title(
                        acc["session_str"], ch_id, value, _acc=acc,
                        access_hash=_pair_hash, username=_pair_uname,
                    )
                    if ok:
                        ok_list.append(f"✅ {chan_title} → {_html.escape(value[:40])}")
                        try:
                            await pool.execute(
                                "UPDATE managed_channels SET title=$1 WHERE owner_id=$2 AND channel_id=$3",
                                value, owner_id, ch_id,
                            )
                        except Exception as e:
                            log_exc_swallow(log, f"bulk_chan_exec: persist title failed for ch={ch_id}: {e}")
                    else:
                        err_list.append(f"❌ {chan_title}: ошибка смены названия")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exc_swallow(log, "_exec_bulk_chan_exec pair=%s: %s", ch_id, exc)
                err_list.append(f"❌ {chan_title}: исключение")

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            await asyncio.sleep(2)
    finally:
        await release_accounts(claimed_ids)

    op_label = {"chan_uname": "🔤 Username", "chan_about": "📄 Описание", "chan_title": "📛 Название"}.get(op, op)
    summary_lines = [
        f"{op_label} — завершено: ✅ {len(ok_list)} ❌ {len(err_list)} из {total}"
        + (f" · пропущено (аккаунт занят): {_busy}" if _busy else "")
    ] + (ok_list + err_list)[:40]
    summary = "\n".join(summary_lines)

    return {
        "status": "done",
        "ok": len(ok_list),
        "fail": len(err_list),
        "summary": summary[:500],
    }


async def _exec_bulk_post_chans(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Post text to multiple channels belonging to one account."""
    from services import account_manager
    from database import db as _db
    from bot.utils.op_helpers import backoff
    # Anti-detection: свой вариант текста в каждый канал (spintax; no-op без него).
    from services.dm_engine import expand_spintax as _expand_spintax

    acc_id = int(params.get("acc_id", 0))
    channel_ids: list[int] = [int(x) for x in (params.get("channel_ids") or [])]
    text: str = params.get("text", "")

    if not acc_id or not channel_ids or not text:
        return {"status": "failed", "reason": "Не указан аккаунт, каналы или текст"}

    row = await _safe_fetchrow(
            pool,
        "SELECT id, session_str, first_name, phone, device_model, system_version, app_version, "
        "lang_code, system_lang_code, "
        "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE AND session_str IS NOT NULL",
        acc_id, owner_id,
    )
    if not row:
        return {"status": "failed", "reason": "Аккаунт не найден или неактивен"}
    acc = dict(row)

    # Отказной захват: постинг идёт живой сессией этого аккаунта.
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "reason": "Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        # Риск-пульс: аккаунт под недавним серьёзным ограничением — публикация с него
        # = быстрый бан. Останавливаем ради защиты (снимется автоматически). fail-open.
        try:
            if await _infra_mem.is_account_quarantined(pool, acc_id):
                return {"status": "failed", "reason": "Аккаунт под риск-пульсом (недавнее "
                        "ограничение) — публикация остановлена для защиты от бана. Повторите позже."}
        except Exception:
            log_exc_swallow(log, f"bulk_post_chans op={op_id}: quarantine check failed")

        # Fetch channels with access_hash and username from DB
        ch_rows = await _safe_fetch(
                pool,
            "SELECT id, channel_id, access_hash, username FROM managed_channels "
            "WHERE owner_id=$1 AND id = ANY($2::bigint[])",
            owner_id, channel_ids,
        )
        channels = [dict(r) for r in ch_rows]
        if not channels:
            return {"status": "failed", "reason": "Каналы не найдены в БД"}

        total = len(channels)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

        ok_count = 0
        err_count = 0
        attempt = 0
        last_result: dict = {}

        for idx, ch in enumerate(channels):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": err_count,
                    "summary": f"Отменено. Опубликовано: {ok_count}/{total}",
                }

            ch_id = ch["channel_id"]
            access_hash = ch.get("access_hash", 0) or 0
            ch_username = ch.get("username") or ""
            _body = _expand_spintax(text)  # свой вариант текста в этот канал
            try:
                last_result = await account_manager.post_to_channel(
                    acc["session_str"], ch_id, _body,
                    access_hash=access_hash, username=ch_username, _acc=acc
                )
                if last_result.get("banned"):
                    await _db.deactivate_account(pool, acc_id, "banned detected in bulk_post_chans")
                    err_count += 1
                elif "error" in last_result:
                    err_count += 1
                else:
                    ok_count += 1
                    # Persist resolved access_hash for future fast-path
                    _rhash = last_result.get("resolved_access_hash", 0)
                    if _rhash and not access_hash:
                        try:
                            await pool.execute(
                                "UPDATE managed_channels SET access_hash=$1 "
                                "WHERE owner_id=$2 AND channel_id=$3 AND (access_hash IS NULL OR access_hash=0)",
                                _rhash, owner_id, int(ch_id),
                            )
                        except Exception as e:
                            log_exc_swallow(log, f"bulk_post_chans: persist access_hash failed for ch={ch_id}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exc_swallow(log, "_exec_bulk_post_chans ch=%s: %s", ch_id, exc)
                err_count += 1
                last_result = {}

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            # Тот же класс, что в bulk_post: max() не ограничивал flood_wait.
            slept = await bounded_flood_sleep(
                last_result.get("flood_wait", 0) or 0, "bulk_publish")
            await asyncio.sleep(max(0.0, backoff(attempt, base=2.0, cap=30.0) - slept))

        return {
            "status": "done",
            "ok": ok_count,
            "fail": err_count,
            "summary": f"📤 Публикация в {total} каналов: ✅ {ok_count} ❌ {err_count}",
        }
    finally:
        await release_accounts([_claimed_acc])


async def _exec_channel_import_all(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Импорт каналов со всех (или указанных) аккаунтов в managed_channels."""
    from services import account_manager
    from database.db import add_managed_channels

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    _CHANNEL_TYPES = ("channel", "megagroup", "supergroup", "gigagroup")

    if account_ids:
        rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    else:
        rows = await resource_selector.select_all_active(
        pool, owner_id, min_trust_score=0.0)
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Нет активных аккаунтов с сессией"}

    # Отказной захват: каждый аккаунт работает своей живой сессией. Без захвата
    # он мог параллельно вести другую операцию → две сессии на одном auth-key.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        n = len(accounts)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", n, op_id)

        total_imported = 0
        total_foreign = 0  # чужие каналы (аккаунт — лишь подписчик), их не тянем
        errors: list[str] = []

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "imported": total_imported,
                    "summary": f"Отменено. Импортировано: {total_imported} каналов из {idx}/{n} аккаунтов",
                }
            try:
                dialogs = await account_manager.get_dialogs(acc["session_str"], limit=200, _acc=acc) or []
                typed = [d for d in dialogs if d.get("type") in _CHANNEL_TYPES]
                # «Моя инфраструктура» = каналы, где аккаунт создатель или админ.
                # Каналы-подписки (участник) — чужие, в managed_channels не тянем,
                # иначе список «Мои каналы» распухает чужими подписками.
                channels = [d for d in typed if d.get("is_admin") or d.get("is_creator")]
                total_foreign += len(typed) - len(channels)
                if channels:
                    # add_managed_channels — НЕ upsert_managed_channels(): get_dialogs(limit=200)
                    # отдаёт максимум 200 ДИАЛОГОВ (не 200 каналов), поэтому channels — частичный
                    # срез при >200 диалогов у аккаунта. upsert_managed_channels() удалила бы
                    # ВСЕ ранее сохранённые каналы аккаунта перед вставкой этого среза.
                    await add_managed_channels(pool, owner_id, acc["id"], channels)
                    total_imported += len(channels)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("_exec_channel_import_all acc=%s: %s", acc.get("id"), exc)
                acc_label = acc.get("first_name") or acc.get("phone") or str(acc["id"])
                errors.append(f"• {acc_label}: {str(exc)[:60]}")

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < n - 1:
                await session_simulator.short_pause(1.5, 3.0)

        err_hint = f"\n⚠️ Ошибок по аккаунтам: {len(errors)}" if errors else ""
        foreign_hint = f"\n🚫 Пропущено чужих (только подписка): {total_foreign}" if total_foreign else ""
        return {
            "status": "done",
            "imported": total_imported,
            "foreign_skipped": total_foreign,
            "accounts": n,
            "summary": f"📡 Импорт каналов: {total_imported} из {n} аккаунтов{foreign_hint}{err_hint}",
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_channel_add(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Вступить в канал/группу по ссылке/username одним аккаунтом и добавить в managed_channels."""
    from services import account_manager

    link = (params.get("channel_identifier") or "").strip()
    if not link:
        return {"status": "failed", "summary": "Не указана ссылка на канал"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, action_type="join", respect_daily_budget=True,
    )
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет свободных активных аккаунтов"}

    acc = dict(accounts[0])
    try:
        res = await account_manager.join_channel(acc["session_str"], link, _acc=acc)
        if "error" in res:
            return {"status": "failed", "summary": f"❌ {res['error']}"}
        title = res.get("title") or link
        # Точечный upsert одной строки — НЕ upsert_managed_channels(), т.к. та
        # функция удаляет ВСЕ существующие каналы аккаунта перед вставкой
        # (рассчитана на полный ре-импорт, а не на добавление одного канала).
        await pool.execute(
            """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username, access_hash, type, members_count)
               VALUES($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (owner_id, channel_id) DO UPDATE
               SET title=EXCLUDED.title, username=EXCLUDED.username,
                   acc_id=EXCLUDED.acc_id, access_hash=EXCLUDED.access_hash,
                   type=EXCLUDED.type,
                   members_count=CASE WHEN EXCLUDED.members_count > 0
                                       THEN EXCLUDED.members_count
                                       ELSE managed_channels.members_count END""",
            owner_id, int(acc["id"]), res["channel_id"], title,
            res.get("username") or "", res.get("access_hash") or 0,
            res.get("type") or "channel", int(res.get("members") or 0),
        )
        return {
            "status": "done",
            "channel_id": res["channel_id"],
            "title": title,
            "summary": f"✅ Канал «{title}» добавлен в управление",
        }
    finally:
        await release_accounts([int(acc["id"])])


async def _exec_check_accounts_health(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Проверить статус всех (или указанных) аккаунтов через Telethon."""
    from services.account_manager import (
        check_account_status_full,
        should_persist_account_status,
    )
    from database import db as _db

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    check_spambot = bool(params.get("check_spambot", True))

    # Включаем device-fingerprint и proxy_url, чтобы проверка статуса шла через
    # привязанный к аккаунту прокси (иначе подключение с серверного IP искажает
    # результат и может триггерить флаги безопасности Telegram).
    _HC_COLS = """
        SELECT a.id, a.session_str, a.first_name, a.phone, a.username,
               a.device_model, a.system_version, a.app_version,
               a.lang_code, a.system_lang_code,
               a.proxy_id, p.proxy_url, p.geo_country
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
    """
    # Минимальный безопасный набор колонок — на случай лага миграции device-полей
    # (app_version/system_lang_code добавлены поздно). Device-fingerprint для
    # проверки статуса не критичен, поэтому фолбэк без него лучше, чем ложное
    # «Нет аккаунтов» (было: _safe_fetch глушил UndefinedColumnError → [] → failed).
    _HC_MIN = """
        SELECT a.id, a.session_str, a.first_name, a.phone, a.username,
               p.proxy_url
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
    """
    # account_ids ВСЕГДА формируются на сервере с проверкой прав (owner-scoped для
    # обычного пользователя, межтенантно для админа — см. accounts_check/_build).
    # Поэтому по явному списку фильтруем ТОЛЬКО по id, без owner_id: иначе админская
    # проверка чужих аккаунтов давала owner_id=админ AND id IN(чужие) = 0 строк →
    # операция падала «0/N» (ровно баг «Проверка 6 аккаунтов — 0/6 Ошибка»).
    _where = ("WHERE a.id = ANY($1::bigint[])"
              if account_ids else "WHERE a.owner_id=$1")
    _args = (account_ids,) if account_ids else (owner_id,)
    query_errored = False
    try:
        rows = await pool.fetch(_HC_COLS + _where, *_args)
    except Exception as exc:
        # Полный набор колонок не прошёл (лаг миграции device-полей и т.п.) —
        # пробуем минимальный. Не глушим совсем: если и он падает — это реальная
        # ошибка БД, её и вернём (а не ложное «нет аккаунтов»).
        log.warning("check_accounts_health: full column set failed (%s), fallback to minimal", exc)
        try:
            rows = await pool.fetch(_HC_MIN + _where, *_args)
        except Exception as exc2:
            return {"status": "failed", "reason": f"Ошибка запроса аккаунтов: {str(exc2)[:100]}"}
        query_errored = True
    accounts = [dict(r) for r in rows]
    if not accounts:
        # Тут аккаунтов реально нет (запрос отработал) — либо у владельца их нет,
        # либо переданы чужие id. Не путаем с ошибкой запроса (см. выше).
        return {"status": "failed", "reason": "Нет аккаунтов для проверки"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    n = len(accounts)
    # done_items=0 при старте: операция может быть перезапущена (retry_count через
    # _maybe_requeue или подхват воркером) с тем же op_id — без сброса счётчик
    # накапливался поверх прошлого прогона (баг «34/17»: done>total).
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1, done_items=0 WHERE id=$2", n, op_id)

    status_counts: dict[str, int] = {}
    deactivated = 0
    reactivated = 0
    errors = 0
    # Разбивка спам-блока на временный/вечный (из spambot-ответа), чтобы
    # результат проверки показывал их раздельно — как отдельные счётчики/папки
    # «Временный/Вечный спамблок» в панели. Общий status остаётся 'spamblock'.
    spamblock_temp = 0
    spamblock_perm = 0

    # Читаемые подписи статусов — используются и для per-account лога (секция
    # «📋 Лог» в деталях операции), и для итоговой сводки ниже.
    _STATUS_LABELS = {
        "active": "✅ активен",
        "spamblock": "🚫 спам-блок",
        "banned": "❌ заблокирован",
        "cooldown": "⏳ FloodWait",
        "session_expired": "🔑 сессия истекла",
        "no_session": "⚪ нет сессии",
        "unknown": "❓ ошибка проверки",
    }
    # Статусы, считающиеся «здоровыми» для бейджа лога (ok vs error).
    _HEALTHY = {"active"}

    for idx, acc in enumerate(accounts):
        if await _is_cancelled(pool, op_id):
            return {
                "status": "cancelled",
                "checked": idx,
                "summary": f"Отменено. Проверено: {idx}/{n}",
            }

        session_str = acc.get("session_str") or ""
        result: dict = {"status": "no_session", "reason": ""}
        try:
            result = await check_account_status_full(
                session_str, _acc=acc, check_spambot=check_spambot
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("_exec_check_accounts_health acc=%s: %s", acc.get("id"), exc)
            result = {"status": "unknown", "reason": f"Ошибка: {str(exc)[:60]}"}
            errors += 1

        status = result.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "spamblock":
            if result.get("spamblock_kind") == "temp":
                spamblock_temp += 1
            else:
                spamblock_perm += 1

        # Профильные факты (Premium, аватар) сохраняем, когда проверка их реально
        # добыла. Отдельно ради них аккаунт не дёргаем: это лишний коннект, то
        # есть лишний след. Пишем только при наличии `profile` — иначе NULL
        # означал бы «нет Premium», хотя на деле это «не проверяли», и риск-движок
        # штрафовал бы аккаунт за пробел в НАШИХ данных.
        _prof = result.get("profile")
        if isinstance(_prof, dict):
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET is_premium=$2, has_photo=$3, "
                    "profile_checked_at=NOW() WHERE id=$1",
                    acc["id"], bool(_prof.get("is_premium")), bool(_prof.get("has_photo")),
                )
            except Exception as e:
                log.warning("check_accounts_health: profile save acc %s failed: %s",
                            acc["id"], e)

        if result.get("auth_error"):
            try:
                await pool.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc["id"])
                deactivated += 1
            except Exception as e:
                log.warning("check_accounts_health: deactivate acc %s failed: %s", acc["id"], e)
        elif status == "active":
            # Подтверждённо рабочий аккаунт (get_me прошёл, ограничений нет):
            # вернуть в строй, если был ошибочно деактивирован разовой auth-ошибкой
            # или сменой прокси. Без реактивации деактивированный аккаунт навсегда
            # оставался "Выкл", даже когда снова рабочий — пользователь видел
            # меньше аккаунтов, чем реально доступно.
            try:
                _res = await pool.execute(
                    "UPDATE tg_accounts SET acc_status='active', status_reason=NULL, "
                    "is_active=TRUE WHERE id=$1 AND is_active=FALSE",
                    acc["id"],
                )
                if str(_res).endswith(" 1"):
                    reactivated += 1
                else:
                    await _db.update_acc_status(pool, acc["id"], status, result.get("reason", ""))
            except Exception as e:
                log.warning("check_accounts_health: reactivate/update acc %s failed: %s", acc["id"], e)
        elif should_persist_account_status(
            status,
            auth_error=bool(result.get("auth_error", False)),
            has_session=bool(session_str),
        ):
            try:
                await _db.update_acc_status(pool, acc["id"], status, result.get("reason", ""))
            except Exception as e:
                log.warning("check_accounts_health: update_acc_status for acc %s failed: %s", acc["id"], e)

        # Per-account запись в лог операции — чтобы секция «📋 Лог» в деталях
        # (mini-app) показывала вердикт по каждому аккаунту, а не пустоту.
        _acc_label = acc.get("first_name") or acc.get("phone") or f"acc#{acc['id']}"
        _verdict = _STATUS_LABELS.get(status, status)
        _reason = (result.get("reason") or "").strip()
        _log_msg = f"{_verdict}{(' — ' + _reason) if _reason else ''}"
        await _safe_execute(
            pool,
            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
            "VALUES($1,$2,$3,$4,$5)",
            op_id, idx + 1, str(_acc_label)[:64],
            "ok" if status in _HEALTHY else "error", _log_msg[:200],
        )

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

    parts = [f"{_STATUS_LABELS.get(s, s)}: {c}" for s, c in sorted(status_counts.items())]
    # Уточняем строку спам-блока разбивкой врем./вечный, если она была.
    if spamblock_temp or spamblock_perm:
        parts = [
            p + f" (врем.: {spamblock_temp}, вечн.: {spamblock_perm})"
            if p.startswith(_STATUS_LABELS["spamblock"]) else p
            for p in parts
        ]
    deact_note = f"\n🔒 Деактивировано: {deactivated}" if deactivated else ""
    react_note = f"\n🔄 Восстановлено: {reactivated}" if reactivated else ""
    summary = f"🔍 Проверено {n} аккаунтов\n" + "\n".join(parts) + deact_note + react_note

    # Persist health snapshots immediately so health_dashboard trends show current data
    # without waiting for the hourly run_health_check_loop cycle.
    try:
        from services import account_health as _ah
        await _ah.load_from_db(pool, owner_id)
        await _ah._persist_health_snapshots(pool)
    except Exception as _he:
        log.debug("_exec_check_accounts_health: health snapshot persist failed: %s", _he)

    return {
        "status": "done",
        "checked": n,
        "deactivated": deactivated,
        "reactivated": reactivated,
        "status_counts": status_counts,
        "spamblock_temp": spamblock_temp,
        "spamblock_perm": spamblock_perm,
        "summary": summary,
    }


async def _exec_connect_discovered_bots(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Подключить найденных на флоте ботов пакетом: токен от BotFather → база.

    Без этого скан был тупиком: найденного бота видно, а подключить нечем —
    токена у него нет. Токен выдаёт сам BotFather по кнопке «API Token», и
    забираем мы его сессией того аккаунта, которому бот принадлежит.

    Токены нигде не логируются; в базу уходят через db.add_bot (шифрование
    token_vault). Уважаем лимит тарифа на число ботов.
    """
    from services import account_manager, bot_api
    from database import db as _db
    from bot.utils.subscription import get_bot_limit, get_effective_bot_count

    only = [str(u).lstrip("@").lower() for u in (params.get("usernames") or [])]
    try:
        cap = max(1, min(50, int(params.get("limit") or 20)))
    except (TypeError, ValueError):
        cap = 20

    rows = await _safe_fetch(pool,
        """SELECT d.username, d.acc_id FROM discovered_bots d
            WHERE d.owner_id=$1 AND d.linked_bot_id IS NULL AND d.acc_id IS NOT NULL
            ORDER BY d.username""", owner_id)
    pending = [dict(r) for r in (rows or [])]
    if only:
        pending = [p for p in pending if str(p["username"]).lower() in set(only)]
    if not pending:
        return {"status": "done", "connected": 0,
                "summary": "🤖 Нечего подключать: все найденные боты уже подключены "
                           "или список пуст — запустите скан флота."}

    # Тарифный потолок: не пытаемся подключать больше, чем разрешено планом.
    try:
        _lim = await get_bot_limit(pool, owner_id)
        _have = await get_effective_bot_count(pool, owner_id)
        room = max(0, int(_lim) - int(_have))
    except Exception:
        room = cap
    if room <= 0:
        return {"status": "failed",
                "summary": "⚠️ Достигнут лимит ботов по тарифу — подключение невозможно"}
    pending = pending[:min(cap, room)]

    # Группируем по аккаунту-владельцу: один диалог с BotFather на аккаунт.
    by_acc: dict = {}
    for p in pending:
        by_acc.setdefault(int(p["acc_id"]), []).append(p["username"])

    acc_rows = await _safe_fetch(pool,
        """SELECT a.id, a.session_str, a.first_name, a.phone, a.username,
                  a.device_model, a.system_version, a.app_version, a.lang_code,
                  a.system_lang_code, a.cf_relay_url, a.proxy_id,
                  p.proxy_url, p.geo_country
             FROM tg_accounts a
             LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
            WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])""",
        owner_id, list(by_acc.keys()))
    accounts = {int(r["id"]): dict(r) for r in (acc_rows or []) if r["session_str"]}
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет живых сессий аккаунтов-владельцев"}

    claimed = await try_claim_accounts(list(accounts.keys()))
    if not claimed:
        return {"status": "requeue", "summary": "⚠️ Аккаунты заняты другой операцией"}

    connected = 0
    failed: list[str] = []
    try:
        await _safe_execute(pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
                            len(pending), op_id)
        import aiohttp as _aio

        done_n = 0
        async with _aio.ClientSession() as _http:
            for acc_id in list(claimed):
                acc = accounts.get(int(acc_id))
                if not acc:
                    continue
                names = by_acc.get(int(acc_id)) or []
                if await _is_cancelled(pool, op_id):
                    break
                try:
                    res = await asyncio.wait_for(
                        account_manager.fetch_bot_tokens_via_botfather(
                            acc["session_str"], names, _acc=acc, limit=len(names)),
                        timeout=60 + 25 * len(names))
                except Exception as e:
                    failed.append(f"аккаунт #{acc_id}: {str(e)[:70]}")
                    continue
                for uname, token in (res.get("tokens") or {}).items():
                    done_n += 1
                    try:
                        info = await bot_api.get_me(_http, token)
                        if not info or not info.get("id"):
                            failed.append(f"@{uname}: токен не принят Telegram")
                            continue
                        added = await _db.add_bot(
                            pool, token=token, bot_id=int(info["id"]),
                            username=info.get("username", "") or uname,
                            first_name=info.get("first_name", "") or "",
                            added_by=owner_id)
                        if added == "taken":
                            failed.append(f"@{uname}: бот уже подключён другим владельцем")
                            continue
                        connected += 1
                        await _safe_execute(
                            pool,
                            "UPDATE discovered_bots SET linked_bot_id=$3 "
                            "WHERE owner_id=$1 AND lower(username)=lower($2)",
                            owner_id, uname, int(info["id"]))
                    except Exception:
                        # Токен в текст ошибки не попадает намеренно.
                        failed.append(f"@{uname}: не удалось подключить")
                for uname, why in (res.get("errors") or {}).items():
                    if uname != "_":
                        failed.append(f"@{uname}: {why}")
                await _safe_execute(pool, "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                                    done_n, op_id)
                await asyncio.sleep(random.uniform(3.0, 6.0))
    finally:
        await release_accounts(list(claimed))

    summary = f"🤖 Подключено ботов: {connected} из {len(pending)}"
    if failed:
        summary += "\nНе удалось:\n" + "\n".join(f"• {x}" for x in failed[:15])
    return {"status": "done", "connected": connected, "summary": summary}


async def _exec_enable_bot_to_bot(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массово включить режим bot-to-bot у управляемых ботов через @BotFather.

    Включить режим может ТОЛЬКО аккаунт-владелец бота (managed_bots.acc_id) —
    API для этого нет. Поэтому группируем ботов по владельцу и прогоняем диалог
    BotFather от каждой сессии. Боты без acc_id включить нечем (нет сессии) —
    честно выводим их в остаток.

    Разбор меню BotFather локале-зависим и на живой сессии не проверен: первый
    прогон — канарейкой на 1–2 ботах (params limit). Пункт не найден → не врём,
    а помечаем «проверьте вручную».
    """
    from services import bot_b2b  # noqa: F401

    only_disabled = params.get("only_disabled", True)
    limit = int(params.get("limit") or 50)
    rows = await _safe_fetch(pool,
        "SELECT bot_id, username, acc_id, COALESCE(b2b_enabled, FALSE) AS b2b_enabled "
        "FROM managed_bots WHERE added_by=$1 AND is_active=TRUE", owner_id)
    bots = [dict(r) for r in (rows or [])]
    if not bots:
        return {"status": "failed", "summary": "⚠️ Нет управляемых ботов"}

    by_acc = bot_b2b.plan_targets(bots, only_disabled=only_disabled, limit=limit)
    no_owner = by_acc.pop(None, [])          # боты без аккаунта-владельца
    if not by_acc:
        if no_owner:
            return {"status": "failed",
                    "summary": f"⚠️ {len(no_owner)} ботов без аккаунта-владельца — "
                    "их режим включается только вручную в @BotFather"}
        return {"status": "done", "summary": "✅ Все боты уже в режиме bot-to-bot"}

    uname_to_bot = {(b.get("username") or "").lstrip("@"): b["bot_id"] for b in bots}
    enabled_n = already_n = 0
    err_lines: list[str] = []
    await _safe_execute(pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
                        sum(len(v) for v in by_acc.values()), op_id)

    for acc_id, unames in by_acc.items():
        if await _is_cancelled(pool, op_id):
            break
        acc = await _db_get_account_for_telethon(pool, int(acc_id), owner_id)
        if not acc or not acc.get("session_str"):
            err_lines.append(f"⚠️ Аккаунт {acc_id}: нет сессии ({len(unames)} ботов)")
            continue
        if not await try_claim_account(int(acc_id)):
            err_lines.append(f"⏳ Аккаунт {acc_id} занят — {len(unames)} ботов пропущены")
            continue
        try:
            res = await asyncio.wait_for(
                bot_b2b.enable_b2b_via_botfather(acc["session_str"], unames,
                                                 _acc=dict(acc), limit=len(unames)),
                timeout=60 + 20 * len(unames))
        except Exception as e:
            err_lines.append(f"⚠️ Аккаунт {acc_id}: {str(e)[:100]}")
            continue
        finally:
            await release_accounts([int(acc_id)])

        done = list(res.get("enabled") or []) + list(res.get("already") or [])
        enabled_n += len(res.get("enabled") or [])
        already_n += len(res.get("already") or [])
        for u in done:
            bid = uname_to_bot.get(u)
            if bid is not None:
                await _safe_execute(pool,
                    "UPDATE managed_bots SET b2b_enabled=TRUE, b2b_checked_at=now() "
                    "WHERE bot_id=$1 AND added_by=$2", bid, owner_id)
        for u, why in (res.get("errors") or {}).items():
            err_lines.append(f"• @{u}: {why}" if u != "_" else f"• {why}")
        await _safe_execute(pool,
            "UPDATE operation_queue SET done_items=done_items+$1 WHERE id=$2",
            len(unames), op_id)

    parts = [f"🕸 Включено bot-to-bot: {enabled_n}"]
    if already_n:
        parts.append(f"уже были: {already_n}")
    if no_owner:
        parts.append(f"без аккаунта-владельца (вручную): {len(no_owner)}")
    summary = " · ".join(parts)
    if err_lines:
        summary += "\n" + "\n".join(err_lines[:15])
    return {"status": "done", "enabled": enabled_n, "already": already_n,
            "summary": summary}


async def _db_get_account_for_telethon(pool, acc_id: int, owner_id: int):
    from database import db as _db
    return await _db.get_account_for_telethon(pool, acc_id, owner_id)


async def _exec_scan_owned_bots(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Найти ботов, которыми владеют аккаунты флота (@BotFather /mybots).

    Разрыв: скан ресурсов находил каналы и чаты, но ботов — никогда (бот не
    Channel и в обходе диалогов не виден). На живом флоте это «164 канала/чата,
    32 аккаунта и 0 ботов», а подключить бота можно было только вручную по
    токену. Здесь — быстрая инвентаризация: что есть и на каком аккаунте.
    Токены не добываются: это отдельный диалог с BotFather на каждого бота.
    """
    from services import account_manager

    account_ids = [int(x) for x in (params.get("account_ids") or [])]
    _COLS = """SELECT a.id, a.session_str, a.first_name, a.phone, a.username,
                      a.device_model, a.system_version, a.app_version,
                      a.lang_code, a.system_lang_code, a.cf_relay_url,
                      a.proxy_id, p.proxy_url, p.geo_country
                 FROM tg_accounts a
                 LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE"""
    if account_ids:
        rows = await _safe_fetch(pool, _COLS + " WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
                                 owner_id, account_ids)
    else:
        rows = await _safe_fetch(pool, _COLS + " WHERE a.owner_id=$1 AND a.is_active=TRUE",
                                 owner_id)
    accounts = [dict(r) for r in (rows or []) if r["session_str"]]
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет аккаунтов с сессией для скана"}

    claimed = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed:
        return {"status": "requeue",
                "summary": "⚠️ Все аккаунты заняты другой операцией — попробуйте позже"}
    accounts = [a for a in accounts if int(a["id"]) in set(claimed)]

    found_total = 0
    new_total = 0
    dead = 0
    lines: list[str] = []
    try:
        await _safe_execute(pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
                            len(accounts), op_id)
        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                break
            label = acc.get("first_name") or acc.get("phone") or f"ID {acc['id']}"
            try:
                res = await asyncio.wait_for(
                    account_manager.scan_owned_bots(acc["session_str"], _acc=acc),
                    timeout=90)
            except Exception as e:
                res = {"bots": [], "error": str(e)[:120]}
            if res.get("error"):
                dead += 1
                lines.append(f"⚠️ {label}: {res['error']}")
            bots = res.get("bots") or []
            found_total += len(bots)
            for uname in bots:
                try:
                    row = await pool.fetchrow(
                        """INSERT INTO discovered_bots(owner_id, acc_id, username)
                           VALUES($1,$2,$3)
                           ON CONFLICT (owner_id, username) DO UPDATE
                             SET last_seen=now(), acc_id=EXCLUDED.acc_id
                           RETURNING (xmax = 0) AS inserted""",
                        owner_id, int(acc["id"]), uname)
                    if row and row["inserted"]:
                        new_total += 1
                except Exception:
                    log.warning("scan_owned_bots: не удалось записать @%s", uname)
            if bots:
                lines.append(f"✅ {label}: {len(bots)} — " + ", ".join("@" + b for b in bots[:8]))
            await _safe_execute(pool, "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                                idx + 1, op_id)
            # BotFather не любит частых обращений подряд — разносим аккаунты.
            await asyncio.sleep(random.uniform(3.0, 7.0))
    finally:
        await release_accounts([int(a["id"]) for a in accounts])

    # Отмечаем уже подключённых: найденный ≠ подключённый, и человек должен
    # видеть, что из найденного уже под управлением.
    try:
        await pool.execute(
            """UPDATE discovered_bots d SET linked_bot_id = mb.bot_id
                 FROM managed_bots mb
                WHERE d.owner_id=$1 AND mb.added_by=$1
                  AND lower(mb.username) = lower(d.username)""", owner_id)
    except Exception:
        log.debug("scan_owned_bots: связывание с managed_bots пропущено")

    summary = (f"🤖 Скан ботов: найдено {found_total} на {len(accounts)} аккаунтах"
               + (f", новых {new_total}" if new_total else "")
               + (f"; {dead} аккаунтов не ответили" if dead else ""))
    if lines:
        summary += "\n" + "\n".join(lines[:20])
    if not found_total:
        summary += ("\nБотов не найдено. Если боты есть — они могли быть созданы "
                    "не с этих аккаунтов: скан спрашивает @BotFather от лица каждого.")
    return {"status": "done", "found": found_total, "new": new_total, "summary": summary}


async def _exec_scan_owned_resources(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Сканировать каналы/группы, где аккаунт является создателем/администратором,
    и импортировать найденные ресурсы в managed_channels с учётом лимита подписки."""
    from services import account_manager
    from bot.utils.subscription import get_channel_limit
    from database import db as _db

    account_ids = [int(x) for x in (params.get("account_ids") or [])]

    _ACCOUNT_COLS = """
        SELECT a.id, a.session_str, a.first_name, a.phone, a.username,
               a.device_model, a.system_version, a.app_version,
               a.lang_code, a.system_lang_code, a.cf_relay_url,
               a.proxy_id, p.proxy_url, p.geo_country
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
    """
    if account_ids:
        rows = await _safe_fetch(
                pool,
            _ACCOUNT_COLS + "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
            owner_id, account_ids,
        )
    else:
        rows = await _safe_fetch(
                pool,
            _ACCOUNT_COLS + "WHERE a.owner_id=$1 AND a.is_active=TRUE",
            owner_id,
        )
    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"status": "failed", "reason": "Нет аккаунтов для сканирования"}

    # Отказной захват: каждый аккаунт работает своей живой сессией.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        n = len(accounts)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", n, op_id)

        chan_limit = await get_channel_limit(pool, owner_id)
        current_count = await _safe_fetchval(
                pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id
        ) or 0
        slots_remaining = chan_limit - int(current_count)

        total_imported = 0
        dead_acc_ids: list[int] = []
        acc_lines: list[str] = []

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "scanned": idx,
                    "imported": total_imported,
                    "summary": f"Отменено. Просканировано: {idx}/{n}, импортировано: {total_imported}",
                }

            acc_id = acc["id"]
            label = (
                acc.get("first_name") or acc.get("username") or acc.get("phone") or f"ID {acc_id}"
            )
            _lines_before = len(acc_lines)

            try:
                session_str = acc.get("session_str") or ""
                result = await account_manager.scan_owned_assets(
                    session_str, _acc=acc
                )
                err = result.get("error")
                owned = result.get("channels", []) + result.get("groups", [])

                if owned:
                    if slots_remaining <= 0:
                        acc_lines.append(f"⛔️ {label}: лимит каналов исчерпан")
                    else:
                        to_import = owned[:slots_remaining]
                        # add_managed_channels — НЕ upsert_managed_channels(): to_import — срез
                        # owned, обрезанный по остатку квоты подписки (slots_remaining), а не
                        # полный список ресурсов аккаунта. upsert_managed_channels() удалила бы
                        # ВСЕ ранее сохранённые каналы аккаунта, если квота урезала to_import
                        # относительно того, что уже было импортировано раньше (например, при
                        # повторном скане после понижения тарифа или когда квоту уже выбрали
                        # другие аккаунты раньше в этом же цикле).
                        imported = await _db.add_managed_channels(pool, owner_id, acc_id, to_import)
                        total_imported += imported
                        slots_remaining -= imported
                        skipped = len(owned) - len(to_import)
                        extra = f", пропущено {skipped} (лимит)" if skipped else ""
                        acc_lines.append(f"✅ {label}: {len(to_import)} ресурсов ({imported} новых{extra})")
                elif err:
                    err_low = err.lower()
                    is_dead = any(
                        x in err_low
                        for x in ("auth", "session", "unauthorized", "key is not registered",
                                  "registered in the system", "authkey", "auth_key")
                    )
                    if is_dead:
                        dead_acc_ids.append(acc_id)
                        try:
                            await pool.execute(
                                "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc_id
                            )
                        except Exception as e:
                            log.warning("scan_owned_resources: deactivate dead acc %s failed: %s", acc_id, e)
                        acc_lines.append(f"🔑 {label}: ключ отозван — нужна переавторизация")
                    elif "flood" in err_low:
                        acc_lines.append(f"⏳ {label}: FloodWait")
                    else:
                        acc_lines.append(f"❌ {label}: {err[:80]}")
                else:
                    acc_lines.append(f"ℹ️ {label}: нет каналов/групп с правами admin/creator")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                exc_s = str(exc).lower()
                if any(x in exc_s for x in ("auth", "key is not registered", "registered in the system")):
                    dead_acc_ids.append(acc_id)
                    try:
                        await pool.execute(
                            "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc_id
                        )
                    except Exception as e:
                        log.warning("scan_owned_resources: deactivate dead acc %s failed: %s", acc_id, e)
                    acc_lines.append(f"🔑 {label}: ключ отозван — нужна переавторизация")
                else:
                    acc_lines.append(f"❌ {label}: {str(exc)[:60]}")

            # Per-account запись в лог операции — раньше acc_lines с детальным
            # результатом по каждому аккаунту НИКУДА не шли (сводка их не включает),
            # т.е. вся детализация вычислялась и терялась. Теперь она видна в секции
            # «📋 Лог» деталей операции. Статус бейджа выводим по эмодзи-префиксу.
            if len(acc_lines) > _lines_before:
                _line = acc_lines[-1]
                if _line.startswith("✅"):
                    _lstatus = "ok"
                elif _line.startswith(("⛔️", "ℹ️")):
                    _lstatus = "skip"
                else:
                    _lstatus = "error"
                await _safe_execute(
                    pool,
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,$4,$5)",
                    op_id, idx + 1, str(label)[:64], _lstatus, _line[:200],
                )

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

        dead_count = len(dead_acc_ids)
        dead_note = f"\n🔑 Мёртвых сессий: {dead_count}" if dead_count else ""
        # Показываем детализацию по аккаунтам прямо в сводке (до 10 строк, остальное —
        # в секции «📋 Лог»). Раньше acc_lines собирались, но нигде не отображались.
        detail = ""
        if acc_lines:
            shown = acc_lines[:10]
            detail = "\n\n" + "\n".join(shown)
            if len(acc_lines) > len(shown):
                detail += f"\n… ещё {len(acc_lines) - len(shown)} (см. лог операции)"
        summary = (
            f"🔎 Просканировано {n} аккаунтов\n"
            f"📡 Импортировано новых ресурсов: {total_imported}{dead_note}{detail}"
        )

        return {
            "status": "done",
            "scanned": n,
            "imported": total_imported,
            "dead": dead_count,
            "summary": summary,
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_reclassify_channels(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Переопределить «мою инфраструктуру»: пройтись живыми сессиями по диалогам,
    проставить каждой строке managed_channels роль (is_admin/is_creator) и убрать
    чужие каналы/группы, где ни один аккаунт не является создателем/админом.

    Безопасность: удаляем ТОЛЬКО строки, которые сессия положительно
    подтвердила как «только участник». Каналы недоступных (мёртвых) сессий и
    любые, что не встретились в диалогах, остаются нетронутыми (роль NULL)."""
    from services import account_manager

    prune = bool(params.get("prune", True))

    accounts = [dict(r) for r in await resource_selector.select_all_active(
        pool, owner_id, min_trust_score=0.0)]
    if not accounts:
        return {"status": "failed", "summary": "Нет активных аккаунтов с сессией"}

    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другой операцией — попробуйте позже"}
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:
        n = len(accounts)
        await _safe_execute(pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2", n, op_id)

        # channel_id → ранг роли по всем аккаунтам: 0=участник, 1=админ, 2=создатель
        rank_map: dict[int, int] = {}
        # channel_id → свежие данные карточки из того же обхода диалогов.
        # Раньше get_dialogs отдавал title/username/members/access_hash, а бралась
        # ОДНА роль — остальное выбрасывалось. Из-за этого список показывал имя и
        # ссылку на момент импорта: переименовал канал в самом Telegram — Infragram
        # об этом не узнавал, — а «участников» стояло 0 у всех, потому что колонку
        # managed_channels.members_count не заполнял никто.
        meta_map: dict[int, dict] = {}
        errors = 0

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                break
            try:
                dialogs = await account_manager.get_dialogs(acc["session_str"], limit=300, _acc=acc) or []
                for d in dialogs:
                    cid = d.get("id")
                    if cid is None:
                        continue
                    cid = int(cid)
                    rank = 2 if d.get("is_creator") else (1 if d.get("is_admin") else 0)
                    rank_map[cid] = max(rank_map.get(cid, 0), rank)
                    _m = meta_map.setdefault(cid, {})
                    # Пустое значение НЕ затирает уже известное: один аккаунт может
                    # видеть карточку беднее другого. Отдельный случай — username:
                    # его могли снять, и это тоже правда, поэтому пустой username
                    # принимается от аккаунта с ролью не ниже уже учтённой.
                    if d.get("title"):
                        _m["title"] = d["title"]
                    if d.get("type"):
                        _m["type"] = d["type"]
                    if d.get("access_hash"):
                        _m["access_hash"] = int(d["access_hash"])
                    # participants_count в списке диалогов — поле необязательное:
                    # Telegram присылает его не всегда. Ноль означает «не сказали»,
                    # а не «никого нет», поэтому нулём известное число не портим.
                    _mem = int(d.get("members") or 0)
                    if _mem > 0:
                        _m["members"] = max(_mem, int(_m.get("members") or 0))
                    if rank >= int(_m.get("_rank", -1)):
                        _m["_rank"] = rank
                        _m["username"] = d.get("username") or ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += 1
                log.warning("_exec_reclassify_channels acc=%s: %s", acc.get("id"), exc)
            await _safe_execute(pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < n - 1:
                await session_simulator.short_pause(1.0, 2.5)

        if not rank_map:
            return {"status": "failed",
                    "summary": "⚠️ Не удалось прочитать диалоги ни одной сессией — роли не изменены"}

        owned_ids = [c for c, r in rank_map.items() if r >= 1]
        creator_ids = [c for c, r in rank_map.items() if r >= 2]
        foreign_ids = [c for c, r in rank_map.items() if r == 0]

        # Карточка канала приводится к тому, что сейчас в Telegram: имя, ссылка,
        # число участников, тип, access_hash. Данные уже добыты обходом выше —
        # ни одного дополнительного запроса к Telegram здесь не делается.
        refreshed = 0
        if meta_map:
            _rows = [
                (
                    owner_id, cid,
                    m.get("title") or None,
                    m.get("username"),                       # '' = ссылку сняли
                    int(m["members"]) if m.get("members") else None,
                    m.get("type") or None,
                    int(m["access_hash"]) if m.get("access_hash") else None,
                )
                for cid, m in meta_map.items()
            ]
            try:
                await pool.executemany(
                    "UPDATE managed_channels SET "
                    "  title         = COALESCE($3, title), "
                    "  username      = COALESCE($4, username), "
                    "  members_count = COALESCE($5, members_count), "
                    "  type          = COALESCE($6, type), "
                    "  access_hash   = COALESCE($7, access_hash) "
                    "WHERE owner_id=$1 AND channel_id=$2",
                    _rows,
                )
                refreshed = len(_rows)
            except Exception as _mexc:
                # Обновление карточек — не причина заваливать переопределение ролей.
                log.warning("_exec_reclassify_channels: карточки не обновлены: %s", _mexc)

        # Проставляем роль по подтверждённым каналам.
        if owned_ids:
            await _safe_execute(
                pool,
                "UPDATE managed_channels SET is_admin=TRUE "
                "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
                owner_id, owned_ids,
            )
        if creator_ids:
            await _safe_execute(
                pool,
                "UPDATE managed_channels SET is_creator=TRUE "
                "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
                owner_id, creator_ids,
            )
        removed = 0
        if foreign_ids:
            await _safe_execute(
                pool,
                "UPDATE managed_channels SET is_admin=FALSE "
                "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
                owner_id, foreign_ids,
            )
            if prune:
                res = await _safe_fetchval(
                    pool,
                    "WITH d AS (DELETE FROM managed_channels "
                    "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[]) RETURNING 1) "
                    "SELECT COUNT(*) FROM d",
                    owner_id, foreign_ids,
                )
                removed = int(res or 0)

        kept = len(owned_ids)
        err_note = f"\n⚠️ Сессий с ошибкой: {errors}" if errors else ""
        upd_note = f"\n🔄 Обновлено карточек: {refreshed}" if refreshed else ""
        if prune:
            summary = (f"✅ Инфраструктура переопределена\n"
                       f"👑 Моих каналов/групп: {kept}\n"
                       f"🚫 Убрано чужих (только участник): {removed}{upd_note}{err_note}")
        else:
            summary = (f"✅ Список обновлён\n"
                       f"👑 Моих: {kept} · 🚫 Чужих помечено: {len(foreign_ids)}"
                       f"{upd_note}{err_note}")
        return {
            "status": "done",
            "owned": kept,
            "foreign_removed": removed,
            "refreshed": refreshed,
            "summary": summary,
        }
    finally:
        await release_accounts(claimed_ids)


async def _exec_deploy_network(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Развернуть связку (Фаза 2): по плану создать недостающие узлы-каналы/группы
    через фабрику, записать их id обратно в узлы, и по рёбрам «admin» назначить
    бот-/аккаунт-узлы администраторами созданных каналов. Под губернатором.

    Боты автоматически не создаются (нужен @BotFather) — такие узлы помечаются как
    требующие ручного шага. Каждый узел/ребро изолирован (fail-open по элементу).
    Использует обычный безопасный путь аккаунт-сессии — новых параллельных
    подключений не вводит.
    """
    from services import network_builder as _nb, account_manager
    try:
        instance_id = int(params.get("instance_id"))
    except (TypeError, ValueError):
        return {"status": "failed", "summary": "⚠️ Связка: не указан instance_id"}
    d = await _nb.get_instance_detail(pool, owner_id, instance_id)
    if not d:
        return {"status": "failed", "summary": "⚠️ Связка не найдена"}
    nodes, edges = d["nodes"], d["edges"]

    # Аккаунт-создатель: явный или первый живой с сессией.
    acc = None
    want_acc = params.get("account_id")
    if want_acc:
        # Оператор указал конкретный аккаунт — грузим ровно его (аккаунт уже
        # выбран, флуд-выбор не нужен). Каноническая загрузка по id даёт полный
        # транспорт (owner_id + cf_relay_url).
        from database import db as _db
        acc = await _db.get_account_for_telethon(pool, int(want_acc), owner_id)
    else:
        # Одна дверь: аккаунт не указан — берём через флуд-осознанный выбор,
        # а не первый попавшийся по id (мимо кулдауна/мёртвых статусов).
        from services import resource_selector as _rsel
        acc = await _rsel.select_account(pool, owner_id, min_trust_score=0.0)
    if not acc:
        return {"status": "failed", "summary": "⚠️ Связка: нет активного аккаунта с сессией"}

    # Отказной захват: операция работает живой сессией аккаунта. Без захвата он
    # мог параллельно вести другую операцию → две сессии на одном auth-key.
    if not await try_claim_account(int(acc["id"])):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(acc["id"])

    try:

        node_ref: dict[int, int] = {n["id"]: n["ref_id"] for n in nodes if n.get("ref_id")}
        node_type: dict[int, str] = {n["id"]: (n.get("node_type") or "").lower() for n in nodes}
        created = wired = 0
        manual: list[str] = []
        for n in nodes:
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "created": created, "wired": wired,
                        "summary": f"Отменено. Создано {created}, связано {wired}."}
            if n.get("ref_id"):
                continue
            ntype = (n.get("node_type") or "").lower()
            label = (n.get("label") or "").strip() or "Объект"
            if ntype in ("channel", "group", "chat"):
                try:
                    res = await account_manager.create_channel(
                        acc["session_str"], label,
                        megagroup=ntype in ("group", "chat"), _acc=acc)
                except Exception as e:
                    manual.append(f"{label}: ошибка создания ({str(e)[:60]})")
                    continue
                ch_id = res.get("channel_id") if isinstance(res, dict) else None
                if not ch_id or res.get("error"):
                    manual.append(f"{label}: {str((res or {}).get('error') or 'не создан')[:60]}")
                    continue
                await _safe_execute(
                    pool,
                    "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username, access_hash, type) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(owner_id, channel_id) DO UPDATE "
                    "SET title=$4, access_hash=$6, type=$7",
                    owner_id, acc["id"], ch_id, label, res.get("username") or None,
                    int(res.get("access_hash") or 0),
                    res.get("type") or ("group" if ntype in ("group", "chat") else "channel"))
                await _nb.update_node_status(pool, n["id"], "created", ref_id=ch_id)
                node_ref[n["id"]] = ch_id
                created += 1
                await _governed_sleep(pool, owner_id, random.uniform(30, 60))
            elif ntype in ("node", "community"):
                # Нода-сообщество = форум-супергруппа + регистрация community_node.
                try:
                    res = await account_manager.create_forum_supergroup(
                        acc["session_str"], label, _acc=acc)
                except Exception as e:
                    manual.append(f"{label}: ошибка создания ноды ({str(e)[:60]})")
                    continue
                ch_id = res.get("channel_id") if isinstance(res, dict) else None
                if not ch_id or res.get("error"):
                    manual.append(f"{label}: {str((res or {}).get('error') or 'нода не создана')[:60]}")
                    continue
                from services import nodes_engine as _ne
                cnode = await _ne.register_community_node(pool, owner_id, int(ch_id), label)
                # Аккаунт-создатель = владелец супергруппы → пишем его админом ноды,
                # чтобы назначение стаффа (community_set_staff) было кому промоутить.
                try:
                    await _ne.add_node_member(pool, int(cnode["id"]), int(acc["id"]), "admin")
                except Exception:
                    pass
                await _nb.update_node_status(pool, n["id"], "created", ref_id=int(ch_id))
                node_ref[n["id"]] = int(ch_id)
                created += 1
                await _governed_sleep(pool, owner_id, random.uniform(30, 60))
            elif ntype == "bot":
                manual.append(f"{label}: бота создайте через @BotFather (Manager Mode)")

        # Рёбра: admin — назначить узел-источник администратором узла-канала;
        # attach/link — прикрепить группу как чат обсуждений к каналу. Всё делает
        # аккаунт-создатель (владелец созданных объектов). crosspost — нативного API
        # нет, помечаем как ручной шаг.
        for e in edges:
            etype = (e.get("edge_type") or "").lower()
            if await _is_cancelled(pool, op_id):
                break
            s_id, t_id = e.get("source_node_id"), e.get("target_node_id")
            src_ref, dst_ref = node_ref.get(s_id), node_ref.get(t_id)
            if etype == "admin":
                if not dst_ref or not src_ref:
                    continue
                try:
                    if await account_manager.promote_to_admin(
                            acc["session_str"], dst_ref, int(src_ref), _acc=acc):
                        wired += 1
                        await _governed_sleep(pool, owner_id, random.uniform(10, 25))
                except Exception as ex:
                    log.debug("_exec_deploy_network admin op=%d: %s", op_id, ex)
            elif etype in ("attach", "link"):
                if not src_ref or not dst_ref:
                    continue
                # Канал = broadcast-узел, группа = megagroup-узел. Определяем по типу.
                if node_type.get(s_id) in ("group", "chat") and node_type.get(t_id) == "channel":
                    channel_ref, group_ref = dst_ref, src_ref
                else:
                    channel_ref, group_ref = src_ref, dst_ref
                try:
                    if await account_manager.set_discussion_group(
                            acc["session_str"], channel_ref, group_ref, _acc=acc):
                        wired += 1
                        await _governed_sleep(pool, owner_id, random.uniform(10, 25))
                except Exception as ex:
                    log.debug("_exec_deploy_network attach op=%d: %s", op_id, ex)
            elif etype == "crosspost":
                # Нативного кросспоста нет — создаём правило пересылки src → dst.
                # Запускается по требованию/расписанию (op crosspost_run).
                if not src_ref or not dst_ref:
                    continue
                await _safe_execute(
                    pool,
                    "INSERT INTO crosspost_links(owner_id, source_channel_id, target_channel_id, account_id) "
                    "VALUES($1,$2,$3,$4) ON CONFLICT (owner_id, source_channel_id, target_channel_id) "
                    "DO UPDATE SET enabled=TRUE, account_id=EXCLUDED.account_id",
                    owner_id, int(src_ref), int(dst_ref), acc["id"])
                wired += 1

        try:
            from services.organism import spine
            await spine.emit(pool, owner_id, "network_deployed",
                             {"instance_id": instance_id, "created": created, "wired": wired})
        except Exception:
            pass
        parts = [f"🔗 Связка развёрнута: создано {created}, связано {wired}"]
        if manual:
            parts.append("Вручную: " + "; ".join(manual[:5]))
        return {"status": "done", "created": created, "wired": wired,
                "manual": manual, "summary": " · ".join(parts)}
    finally:
        await release_accounts([_claimed_acc])


async def _exec_community_add_channel(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Ноды-комьюнити: создать канал (форум-топик) в ноде через Bot API.
    Выполняется в процессе бота (у mini_app нет bot-инстанса)."""
    from services import nodes_engine
    try:
        node_id = int(params.get("node_id"))
    except (TypeError, ValueError):
        return {"status": "failed", "summary": "⚠️ Канал ноды: не указан node_id"}
    names = params.get("names") or ([params["name"]] if params.get("name") else [])
    created = 0
    for nm in names[:20]:
        if await _is_cancelled(pool, op_id):
            break
        res = await nodes_engine.create_community_channel(pool, bot, owner_id, node_id, str(nm))
        if res:
            created += 1
            await asyncio.sleep(random.uniform(1.5, 3.5))
    return {"status": "done" if created else "failed", "created": created,
            "summary": f"🖥 Ноды: создано каналов {created}"}


async def _exec_community_liven(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Оживить ноду флотом: N аккаунтов вступают в её супергруппу и становятся
    участниками. Дальше их поддерживает ghost_engine (это их подписка). Под
    губернатором. Роль по умолчанию 'member'."""
    from services import account_manager, nodes_engine
    try:
        node_id = int(params.get("node_id"))
    except (TypeError, ValueError):
        return {"status": "failed", "summary": "⚠️ Оживление: не указан node_id"}
    node = await _safe_fetchrow(
        pool, "SELECT tg_chat_id FROM community_nodes WHERE id=$1 AND owner_id=$2 AND is_active",
        node_id, owner_id)
    if not node:
        return {"status": "failed", "summary": "⚠️ Нода не найдена"}
    try:
        count = max(1, min(int(params.get("count") or 5), 50))
    except (TypeError, ValueError):
        count = 5
    # Одна дверь к аккаунту (шаг №1 аудита): выбор через флуд-осознанный слой
    # вместо сырого random() — не берём аккаунт в кулдауне/мёртвый по статусу и
    # получаем полный транспорт (owner_id + cf_relay_url). min_trust=0.0 —
    # порог доверия здесь не вводим, меняем только «какой аккаунт безопасно взять».
    from services import resource_selector as _rsel
    accs = await _rsel.select_accounts(pool, owner_id, count, min_trust_score=0.0)
    chat_id = int(node["tg_chat_id"])
    # Пригласительная ссылка через бота (он админ ноды) — так вступают и в
    # приватную только что созданную супергруппу (по id без access_hash нельзя).
    invite_link = None
    try:
        inv = await bot.create_chat_invite_link(chat_id=chat_id)
        invite_link = getattr(inv, "invite_link", None)
    except Exception as e:
        log.warning("_exec_community_liven invite link op=%d chat=%s: %s", op_id, chat_id, e)
    if not invite_link:
        return {"status": "failed",
                "summary": "⚠️ Оживление: бот не смог создать ссылку (нужен админ с правом приглашать)"}
    joined = 0
    busy = 0
    for acc in (accs or []):
        if await _is_cancelled(pool, op_id):
            break
        # Захват на КАЖДЫЙ аккаунт: вступает он своей живой сессией. Занятый
        # пропускаем, а не открываем на нём вторую сессию (AUTH_KEY_DUPLICATED).
        if not await try_claim_account(int(acc["id"])):
            busy += 1
            continue
        _claimed_acc = int(acc["id"])
        try:
            res = await account_manager.join_channel(
                acc["session_str"], invite_link, _acc=dict(acc))
        except Exception as e:
            log.debug("_exec_community_liven join op=%d acc=%s: %s", op_id, acc["id"], e)
            continue
        finally:
            # join_channel закрывает сессию сам — держать аккаунт дольше незачем.
            await release_accounts([_claimed_acc])
        if isinstance(res, dict) and not res.get("error"):
            await nodes_engine.add_node_member(pool, node_id, int(acc["id"]), "member")
            joined += 1
            await _governed_sleep(pool, owner_id, random.uniform(20, 45))
    return {"status": "done", "joined": joined,
            "summary": (f"🏛 Оживление ноды: вступило {joined} аккаунтов флота"
                        + (f" · пропущено занятых: {busy}" if busy else ""))}


async def _exec_community_set_staff(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Назначить участников-флот ноды модераторами/админами (роли): промоут в
    супергруппе через владельца ноды + запись роли. account_ids + role."""
    from services import account_manager, nodes_engine
    try:
        node_id = int(params.get("node_id"))
    except (TypeError, ValueError):
        return {"status": "failed", "summary": "⚠️ Роли: не указан node_id"}
    role = params.get("role")
    role = role if role in ("moderator", "admin") else "moderator"
    node = await _safe_fetchrow(
        pool, "SELECT tg_chat_id FROM community_nodes WHERE id=$1 AND owner_id=$2 AND is_active",
        node_id, owner_id)
    if not node:
        return {"status": "failed", "summary": "⚠️ Нода не найдена"}
    acc_ids = [int(x) for x in (params.get("account_ids") or []) if str(x).isdigit()]
    if not acc_ids:
        return {"status": "failed", "summary": "⚠️ Роли: не выбраны аккаунты"}
    # Промоутить может только владелец/админ супергруппы (с правом add_admins).
    # Берём аккаунт-члена ноды с ролью admin (это создатель супергруппы); только
    # если такого нет — откатываемся на любой активный (может не иметь прав).
    owner_acc = await _safe_fetchrow(
        pool, "SELECT a.id, a.session_str, a.device_model, a.system_version, a.app_version, "
        "a.lang_code, a.system_lang_code, a.proxy_id FROM community_node_members m "
        "JOIN tg_accounts a ON a.id=m.account_id "
        "WHERE m.node_id=$1 AND m.role='admin' AND a.is_active AND a.session_str IS NOT NULL "
        "ORDER BY m.joined_at LIMIT 1", node_id)
    if not owner_acc:
        # Одна дверь: фолбэк-промоутер — через флуд-осознанный выбор, а не первый
        # попавшийся по id (не берём аккаунт в кулдауне/мёртвый по статусу).
        from services import resource_selector as _rsel
        owner_acc = await _rsel.select_account(pool, owner_id, min_trust_score=0.0)
    if not owner_acc:
        return {"status": "failed", "summary": "⚠️ Роли: нет аккаунта-промоутера"}

    # Отказной захват: операция работает живой сессией этого аккаунта.
    # Без захвата он мог параллельно вести другую операцию (две сессии на
    # одном auth-key → AUTH_KEY_DUPLICATED).
    if not await try_claim_account(int(owner_acc["id"])):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(owner_acc["id"])

    try:
        chat_id = int(node["tg_chat_id"])
        promoted = 0
        for aid in acc_ids:
            if await _is_cancelled(pool, op_id):
                break
            target = await _safe_fetchrow(
                pool, "SELECT tg_user_id FROM tg_accounts WHERE id=$1 AND owner_id=$2", aid, owner_id)
            uid_tg = target["tg_user_id"] if target else None
            if not uid_tg:
                continue
            try:
                ok = await account_manager.promote_to_admin(
                    owner_acc["session_str"], chat_id, int(uid_tg), _acc=dict(owner_acc),
                    ban_users=True, delete_messages=True, pin_messages=True,
                    add_admins=(role == "admin"))
                if ok:
                    await nodes_engine.add_node_member(pool, node_id, aid, role)
                    promoted += 1
                    await _governed_sleep(pool, owner_id, random.uniform(10, 25))
            except Exception as e:
                log.debug("_exec_community_set_staff op=%d acc=%s: %s", op_id, aid, e)
        return {"status": "done", "promoted": promoted,
                "summary": f"🛡 Роли ноды: назначено {promoted} ({role})"}
    finally:
        await release_accounts([_claimed_acc])


async def _exec_crosspost_run(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Связки 2c: прогнать кросспостинг — переслать новые посты по всем активным
    правилам владельца (crosspost_links). Под губернатором. Один прогон = одна
    сессия на аккаунт-форвардер (безопасно), курсор last_msg_id двигается вперёд.
    """
    from services import account_manager
    only_id = params.get("link_id")
    if only_id:
        links = await _safe_fetch(
            pool, "SELECT * FROM crosspost_links WHERE owner_id=$1 AND id=$2 AND enabled",
            owner_id, int(only_id))
    else:
        links = await _safe_fetch(
            pool, "SELECT * FROM crosspost_links WHERE owner_id=$1 AND enabled "
            "ORDER BY COALESCE(last_run_at, to_timestamp(0)) ASC LIMIT 50", owner_id)
    if not links:
        return {"status": "done", "summary": "🔁 Кросспостинг: активных правил нет"}
    total_fwd = 0
    runs = 0
    for lk in links:
        if await _is_cancelled(pool, op_id):
            break
        acc = None
        if lk.get("account_id"):
            acc = await _safe_fetchrow(
                pool, "SELECT id, session_str, device_model, system_version, app_version, "
                "lang_code, system_lang_code, proxy_id FROM tg_accounts "
                "WHERE id=$1 AND owner_id=$2 AND is_active AND session_str IS NOT NULL",
                lk["account_id"], owner_id)
        if not acc:
            # Одна дверь: фолбэк-аккаунт для связки — через флуд-осознанный выбор
            # (не берём кулдаун/мёртвый по статусу), а не первый по id.
            from services import resource_selector as _rsel
            acc = await _rsel.select_account(pool, owner_id, min_trust_score=0.0)
        if not acc:
            continue
        # Захват на КАЖДУЮ связку: аккаунт здесь свой у каждой ссылки, поэтому
        # держим его ровно на время пересылки, а не на весь прогон.
        if not await try_claim_account(int(acc["id"])):
            log.info("_exec_crosspost_run: аккаунт %s занят — связка %s пропущена",
                     acc["id"], lk.get("id"))
            continue
        _claimed_acc = int(acc["id"])
        try:
            res = await account_manager.forward_new_posts(
                acc["session_str"], lk["source_channel_id"], lk["target_channel_id"],
                since_msg_id=int(lk["last_msg_id"] or 0), _acc=dict(acc))
        except Exception as e:
            log.debug("_exec_crosspost_run link=%s: %s", lk.get("id"), e)
            continue
        finally:
            # forward_new_posts закрывает сессию сам — держать аккаунт дольше незачем.
            await release_accounts([_claimed_acc])
        fwd = int(res.get("forwarded") or 0)
        total_fwd += fwd
        runs += 1
        await _safe_execute(
            pool, "UPDATE crosspost_links SET last_msg_id=$1, forwarded_total=forwarded_total+$2, "
            "last_run_at=now() WHERE id=$3",
            int(res.get("last_msg_id") or lk["last_msg_id"] or 0), fwd, lk["id"])
        if fwd:
            await _governed_sleep(pool, owner_id, random.uniform(5, 12))
    return {"status": "done", "forwarded": total_fwd, "rules": runs,
            "summary": f"🔁 Кросспостинг: переслано {total_fwd} по {runs} правил."}


async def _exec_promote_all_admins(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Назначить все аккаунты пользователя администраторами указанного канала.

    Использует сессию owner_acc_id как учётную запись с правами admin, которая
    повышает остальные аккаунты. Аккаунты должны уже быть участниками канала.
    """
    from services import account_manager
    from database import db as _db

    owner_acc_id = int(params.get("owner_acc_id", 0))
    channel_id = int(params.get("channel_id", 0))

    if not owner_acc_id or not channel_id:
        return {"status": "failed", "summary": "⚠️ promote_all_admins: не указаны owner_acc_id или channel_id"}

    owner_acc = await _db.get_account_for_telethon(pool, owner_acc_id, owner_id)
    if not owner_acc:
        return {"status": "failed", "summary": "⚠️ promote_all_admins: аккаунт-администратор не найден"}

    accounts = await _safe_fetch(
            pool,
        "SELECT id, phone, first_name, tg_user_id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND tg_user_id IS NOT NULL AND id != $2",
        owner_id, owner_acc_id,
    )
    if not accounts:
        return {"status": "done", "ok": 0, "fail": 0, "summary": "👑 Нет других аккаунтов для назначения"}

    # Отказной захват: операция работает живой сессией этого аккаунта.
    # Без захвата он мог параллельно вести другую операцию (две сессии на
    # одном auth-key → AUTH_KEY_DUPLICATED).
    if not await try_claim_account(int(owner_acc["id"])):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    _claimed_acc = int(owner_acc["id"])

    try:

        n = len(accounts)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", n, op_id)

        ok_count = 0
        fail_count = 0

        for idx, acc in enumerate(accounts):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": fail_count,
                    "summary": f"Отменено. Назначено: {ok_count}/{n}",
                }
            try:
                ok = await account_manager.promote_to_admin(
                    owner_acc["session_str"], channel_id, acc["tg_user_id"], _acc=dict(owner_acc)
                )
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("_exec_promote_all_admins op=%d acc=%s: %s", op_id, acc.get("id"), exc)
                fail_count += 1

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < n - 1:
                # межаккаунтный темп под губернатором (давление флота тормозит)
                await asyncio.sleep(await _governed_delay(pool, owner_id, 2.0))

        summary = (
            f"👑 Назначение администраторов канала\n"
            f"✅ Успешно: {ok_count}/{n}"
            + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
        )
        return {"status": "done", "ok": ok_count, "fail": fail_count, "total": n, "summary": summary}
    finally:
        await release_accounts([_claimed_acc])


# ── Накрутка: просмотры, реакции, сторис ─────────────────────────────────────

async def _record_boost_flood(pool: asyncpg.Pool, acc_id: int, err: str, op_id: int) -> None:
    """Если ошибка аккаунта — FloodWait, выставить cooldown через flood_engine.

    Без этого аккаунт остаётся с cooldown_until=NULL и будет выбран следующей
    накруткой повторно → повторный флуд → риск бана. Записываем штраф один раз
    на аккаунт при флуде.
    """
    if "flood" not in (err or "").lower():
        return
    try:
        from services import boost_engine, flood_engine
        wait = boost_engine.extract_flood_wait(None, err) or 60
        await flood_engine.record_flood(
            pool, int(acc_id), wait, action_type="boost", operation_id=op_id
        )
    except Exception:
        log.debug("boost flood record failed acc=%s", acc_id)


async def _exec_boost_views(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Накрутка просмотров: каждый аккаунт вызывает GetMessagesViewsRequest."""
    from services import boost_engine

    channel = params.get("channel", "")
    msg_ids = [int(i) for i in (params.get("msg_ids") or [])]
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not channel or not msg_ids or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры boost_views"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Anti-detection (#7): отсеять аккаунты в карантине ПЕРЕД действием — действие
    # с флагнутого (флуд/ограничение) аккаунта = быстрый бан. Общий гейт, fail-open.
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count, fail_count = 0, 0
    total = len(accounts)

    for idx, acc in enumerate(accounts, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            res = await boost_engine.boost_views(
                acc["session_str"],
                dict(acc),
                channel,
                msg_ids,
            )
            if res["ok"]:
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                    op_id, idx, f"acc#{acc['id']}",
                )
            else:
                fail_count += 1
                await _record_boost_flood(pool, acc["id"], res.get("error") or "", op_id)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                )
        except Exception as exc:
            log.warning("boost_views op=%d acc=%s: %s", op_id, acc.get("id"), exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 1.5))

    summary = (
        f"👁 Просмотры: {channel} × {len(msg_ids)} сообщений\n"
        f"✅ Аккаунтов: {ok_count}/{total}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
    )
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


async def _exec_boost_reactions(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Накрутка реакций: каждый аккаунт ставит реакцию emoji на msg_id."""
    from services import boost_engine

    channel = params.get("channel", "")
    msg_id = int(params.get("msg_id") or 0)
    emoji = params.get("emoji") or "❤"
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not channel or not msg_id or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры boost_reactions"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Anti-detection (#7): отсеять аккаунты в карантине ПЕРЕД действием — действие
    # с флагнутого (флуд/ограничение) аккаунта = быстрый бан. Общий гейт, fail-open.
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count, fail_count = 0, 0
    total = len(accounts)

    for idx, acc in enumerate(accounts, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            res = await boost_engine.boost_reaction(
                acc["session_str"],
                dict(acc),
                channel,
                msg_id,
                emoji,
            )
            if res["ok"]:
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                    op_id, idx, f"acc#{acc['id']}",
                )
            else:
                fail_count += 1
                await _record_boost_flood(pool, acc["id"], res.get("error") or "", op_id)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                )
        except Exception as exc:
            log.warning("boost_reactions op=%d acc=%s: %s", op_id, acc.get("id"), exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 2.0))

    summary = (
        f"{emoji} Реакции: {channel} сообщение #{msg_id}\n"
        f"✅ Аккаунтов: {ok_count}/{total}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
    )
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


async def _exec_boost_stories(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Просмотр сторис: каждый аккаунт просматривает все активные сторис target."""
    from services import boost_engine

    target = params.get("target", "")
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not target or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры boost_stories"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Anti-detection (#7): отсеять аккаунты в карантине ПЕРЕД действием, fail-open.
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count, fail_count, stories_seen = 0, 0, 0
    total = len(accounts)

    for idx, acc in enumerate(accounts, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            res = await boost_engine.boost_stories(
                acc["session_str"],
                dict(acc),
                target,
            )
            if res["ok"]:
                ok_count += 1
                stories_seen = max(stories_seen, res.get("stories_count", 0))
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                    op_id, idx, f"acc#{acc['id']}",
                )
            else:
                fail_count += 1
                await _record_boost_flood(pool, acc["id"], res.get("error") or "", op_id)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                )
        except Exception as exc:
            log.warning("boost_stories op=%d acc=%s: %s", op_id, acc.get("id"), exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 1.0))

    summary = (
        f"📖 Сторис: {target}"
        + (f" ({stories_seen} шт.)" if stories_seen else "")
        + f"\n✅ Аккаунтов: {ok_count}/{total}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
    )
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


async def _filter_quarantined_accounts(
    pool: asyncpg.Pool, op_id: int, accounts: list
) -> tuple[list, int]:
    """Отсеять аккаунты под риск-пульсом (is_account_quarantined) ПЕРЕД действием.

    Возвращает (оставшиеся_аккаунты, пропущено_шт). Fail-open: если ВСЕ в карантине —
    возвращаем исходный список (лучше рискнуть, чем обнулить операцию); ошибка проверки
    тоже не блокирует ядро. Общий гейт для boost/action-исполнителей по реальным
    аккаунтам — действие с флагнутого аккаунта = быстрый бан.
    """
    try:
        kept = [a for a in accounts
                if not await _infra_mem.is_account_quarantined(pool, a["id"])]
    except Exception:
        log_exc_swallow(log, f"quarantine filter failed op={op_id}")
        return accounts, 0
    if kept and len(kept) != len(accounts):
        skipped = len(accounts) - len(kept)
        log.info("op=%d: пропущено %d аккаунтов в карантине (риск-пульс)", op_id, skipped)
        return kept, skipped
    return accounts, 0


# Сколько раз инвайт вправе продолжить сам себя. Аудитория в 2000 целей при
# консервативном суточном лимите растягивается на дни — но бесконечно хвост
# продлеваться не должен, иначе забытая операция будет жить месяцами.
_MAX_INVITE_CHAIN = 14

# Потолок пула целей, читаемых из источника за одну операцию инвайта. Прежние
# 2000 были зашиты в каждый запрос и служили одновременно потолком и причиной
# ложного тупика «все уже приглашены» (дедуп применялся ПОСЛЕ усечения). Остаток
# уезжает в продолжение (_schedule_invite_continuation), так что больший пул —
# это то, что цепочке нужно дотягивать.
_INVITE_AUDIENCE_CAP = 20000


def _row_to_ref(row) -> tuple:
    """Строка источника аудитории → (цель, ключ источника).

    Одна функция на все источники: у них разные колонки, но одинаковый контракт —
    '@username' | numeric id | '+phone' и метка источника для interleave_by_source.
    (None, None) — строка не даёт годной цели.
    """
    r = dict(row)
    skey = r.get("source_username") or r.get("source_id") or r.get("bot_id")
    uname = r.get("username")
    if uname:
        return "@" + str(uname).lstrip("@"), skey
    for col in ("tg_user_id", "user_id"):
        if r.get(col):
            return r[col], skey
    ph = r.get("phone")
    if ph:
        ph = str(ph).strip()
        return (ph if ph.startswith("+") else "+" + ph), skey
    return None, None


async def _schedule_invite_continuation(
    pool, owner_id: int, params: dict, users: list, phones: list, chain: int
) -> "int | None":
    """Поставить продолжение инвайта на завтра. Возвращает op_id или None.

    Цели передаются ЯВНЫМ списком, а не источником: повторное чтение источника
    захватило бы и тех, кого уже пригласили. Через шину (`operation_bus.submit`),
    а не прямым INSERT — продолжение обязано проходить те же проверки, что и
    обычный запуск.

    Никогда не бросает: продолжение — улучшение, а не обязательство, и его сбой
    не должен превращать успешный прогон в проваленный.
    """
    import datetime as _dt
    try:
        nxt = dict(params)
        nxt.pop("parse_run_id", None)
        nxt["source"] = "import_list"          # аудитория уже зафиксирована
        nxt["user_refs"] = users
        nxt["phones"] = phones
        nxt["invite_chain"] = chain
        # Завтра сразу после полуночи UTC: суточные счётчики считаются по
        # CURRENT_DATE, значит именно тогда лимиты и обновятся.
        when = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)).replace(
            hour=0, minute=10, second=0, microsecond=0)
        from services import operation_bus as _obus
        return await _obus.submit(
            pool, owner_id, "mass_invite", nxt,
            total_items=len(users) + len(phones),
            scheduled_for=when.isoformat(),
            label=f"Инвайт → {params.get('group', '')} (продолжение {chain})",
            # Тарифный гейт продолжение НЕ проходит, и это принципиально: цели
            # здесь — остаток УЖЕ принятой операции, а разбиение по дням наша
            # внутренняя механика (суточный лимит на аккаунт), а не новая покупка.
            # С включённым гейтом хвост аудитории молча переставал приглашаться
            # после первого дня — проверено на живой базе: submit падал с
            # PlanRequiredError, ошибка глоталась, продолжение не создавалось.
            # Новую работу так не начать: сюда попадают только те цели, что
            # операция уже взяла в работу.
            bypass_plan_check=True,
        )
    except Exception as exc:
        # Не тихо: остаток аудитории остаётся неприглашённым, и это должно быть
        # видно в логах, а не только по косвенным признакам.
        log.warning("mass_invite: продолжение не поставлено (owner=%s): %s", owner_id, exc)
        return None


# Сколько раз Strike вправе сам себя переповторить в режиме настойчивой эскалации.
# Реальные тейкдауны идут ВО ВРЕМЕНИ (площадка/модерация реагирует не мгновенно):
# давим повторно, пока цель не снята или пока не исчерпан лимит попыток. Каждый
# круг разнесён на часы — и чтобы дать площадке отреагировать, и чтобы аккаунты
# успевали остыть между заходами.
_MAX_STRIKE_CHAIN = 4
_STRIKE_CONTINUATION_HOURS = 12


async def _schedule_strike_continuation(
    pool, owner_id: int, params: dict, chain: int
) -> "int | None":
    """Поставить следующий заход Strike через ~12ч (настойчивая эскалация).

    Через шину (operation_bus.submit), а не прямым INSERT — заход обязан проходить
    те же проверки, что обычный запуск. Никогда не бросает: эскалация — улучшение,
    её сбой не должен ронять успешный прогон. Аккаунты НЕ фиксируем (account_ids
    сбрасываем) — каждый заход берёт свежий здоровый флот; force=True, т.к. это
    сознательный повтор по уже принятой цели, а не новая покупка.
    """
    import datetime as _dt
    try:
        nxt = dict(params)
        nxt.pop("account_ids", None)     # свежий выбор флота на каждый заход
        nxt["persist"] = True
        nxt["strike_chain"] = chain
        nxt["force"] = True              # обойти restrike-guard: это и есть повтор
        when = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(
            hours=_STRIKE_CONTINUATION_HOURS)
        from services import operation_bus as _obus
        return await _obus.submit(
            pool, owner_id, "strike", nxt,
            total_items=1,
            scheduled_for=when.isoformat(),
            label=f"Strike → {params.get('target', '')} (эскалация {chain})",
            bypass_plan_check=True,
        )
    except Exception as exc:
        log.warning("strike: эскалация не поставлена (owner=%s): %s", owner_id, exc)
        return None


async def _premium_filter_accounts(
    pool: asyncpg.Pool, op_id: int, accounts: list, premium_only: bool
) -> tuple[list, int]:
    """Отфильтровать аккаунты по признаку Premium (если запрошено).

    Не проваливает операцию, если часть аккаунтов не Premium — просто
    исключает их из списка. Возвращает (отфильтрованные_аккаунты, пропущено_шт).
    """
    if not premium_only:
        return accounts, 0
    from services import account_manager

    filtered = []
    skipped = 0
    for acc in accounts:
        if await _is_cancelled(pool, op_id):
            break
        try:
            is_prem = await account_manager.is_premium_account(acc["session_str"], dict(acc))
        except Exception as e:
            log.warning('is_premium_account check failed: %s', e)
            is_prem = False
        if is_prem:
            filtered.append(acc)
        else:
            skipped += 1
    return filtered, skipped


async def _exec_boost_subscribers(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Накрутка подписчиков/участников: каждый аккаунт вступает в канал/группу.

    Переиспользует account_manager.join_channel — уже умеет и публичные
    @username, и приватные t.me/+hash инвайт-ссылки.
    """
    from services import account_manager

    target = params.get("target", "")
    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    premium_only = bool(params.get("premium_only"))

    if not target or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры boost_subscribers"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Отказной захват: каждый аккаунт работает своей живой сессией. Без захвата
    # он мог параллельно вести другую операцию → две сессии на одном auth-key.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        accounts, skipped_premium = await _premium_filter_accounts(pool, op_id, accounts, premium_only)
        if not accounts:
            return {
                "status": "failed",
                "summary": f"⚠️ Нет Premium-аккаунтов среди выбранных ({skipped_premium} пропущено)",
            }
        # Риск-пульс: вступление с флагнутого аккаунта = быстрый бан. Отсеиваем (fail-open).
        accounts, skipped_quar = await _filter_quarantined_accounts(pool, op_id, accounts)
        if skipped_premium or skipped_quar:
            # total_items учитывало все выбранные аккаунты; часть отсеяна фильтрами
            # (Premium/риск-пульс) — подгоняем, иначе прогресс-бар не дойдёт до 100%.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET total_items=$1 WHERE id=$2", len(accounts), op_id
            )

        ok_count, fail_count = 0, 0
        total = len(accounts)

        for idx, acc in enumerate(accounts, 1):
            if await _is_cancelled(pool, op_id):
                break
            try:
                res = await account_manager.join_channel(acc["session_str"], target, _acc=dict(acc))
                if res.get("error"):
                    fail_count += 1
                    await _record_boost_flood(pool, acc["id"], res.get("error") or "", op_id)
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                        op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                    )
                else:
                    ok_count += 1
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                        op_id, idx, f"acc#{acc['id']}",
                    )
            except Exception as exc:
                log.warning("boost_subscribers op=%d acc=%s: %s", op_id, acc.get("id"), exc)
                fail_count += 1

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < total:
                # межцелевой темп накрутки под губернатором (давление флота тормозит)
                await asyncio.sleep(await _governed_delay(pool, owner_id, random.uniform(3.0, 7.0)))

        summary = (
            f"👥 Подписчики/участники: {target}\n"
            f"✅ Вступили: {ok_count}/{total}"
            + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
            + (f"\n💎 Пропущено не-Premium: {skipped_premium}" if skipped_premium else "")
            + (f"\n🛡 Пропущено (риск-пульс): {skipped_quar}" if skipped_quar else "")
        )
        return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}
    finally:
        await release_accounts(claimed_ids)


async def _exec_boost_bot_starts(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Накрутка стартов в ботах: каждый аккаунт отправляет /start (с payload) боту."""
    from services import account_manager

    bot_username = (params.get("bot_username") or "").strip()
    payload = (params.get("payload") or "").strip() or None
    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    premium_only = bool(params.get("premium_only"))

    if not bot_username or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры boost_bot_starts"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Отказной захват: каждый аккаунт работает своей живой сессией. Без захвата
    # он мог параллельно вести другую операцию → две сессии на одном auth-key.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        accounts, skipped_premium = await _premium_filter_accounts(pool, op_id, accounts, premium_only)
        if not accounts:
            return {
                "status": "failed",
                "summary": f"⚠️ Нет Premium-аккаунтов среди выбранных ({skipped_premium} пропущено)",
            }
        # Риск-пульс: /start с флагнутого аккаунта = риск бана. Отсеиваем (fail-open).
        accounts, skipped_quar = await _filter_quarantined_accounts(pool, op_id, accounts)
        if skipped_premium or skipped_quar:
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET total_items=$1 WHERE id=$2", len(accounts), op_id
            )

        ok_count, fail_count = 0, 0
        total = len(accounts)

        for idx, acc in enumerate(accounts, 1):
            if await _is_cancelled(pool, op_id):
                break
            try:
                res = await account_manager.send_bot_start(
                    acc["session_str"], bot_username, payload, _acc=dict(acc)
                )
                if res.get("error"):
                    fail_count += 1
                    await _record_boost_flood(pool, acc["id"], res.get("error") or "", op_id)
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                        op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                    )
                else:
                    ok_count += 1
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                        op_id, idx, f"acc#{acc['id']}",
                    )
            except Exception as exc:
                log.warning("boost_bot_starts op=%d acc=%s: %s", op_id, acc.get("id"), exc)
                fail_count += 1

            await _safe_execute(
                    pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            if idx < total:
                # межцелевой темп накрутки под губернатором (давление флота тормозит)
                await asyncio.sleep(await _governed_delay(pool, owner_id, random.uniform(2.5, 6.0)))

        summary = (
            f"🚀 Старты в боте: @{bot_username}"
            + (f" (payload: {payload})" if payload else "")
            + f"\n✅ Стартов: {ok_count}/{total}"
            + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
            + (f"\n💎 Пропущено не-Premium: {skipped_premium}" if skipped_premium else "")
            + (f"\n🛡 Пропущено (риск-пульс): {skipped_quar}" if skipped_quar else "")
        )
        return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}
    finally:
        await release_accounts(claimed_ids)


# ── Инвайтер ──────────────────────────────────────────────────────────────────

# Дедуп уже-приглашённых: повторный прогон по той же группе не должен снова тыкать
# тех, кого уже обрабатывали — это сжигает суточный лимит и растит PeerFlood.
# Ключ — (владелец, нормализованная группа). Пишем цели, которые РЕАЛЬНО отдали
# движку (успех/отказ равно «уже трогали» — повторно поке не подлежат).
_INVITE_LOG_DDL = (
    "CREATE TABLE IF NOT EXISTS invite_target_log("
    "owner_id BIGINT NOT NULL, group_key TEXT NOT NULL, target TEXT NOT NULL, "
    "op_id BIGINT, created_at TIMESTAMPTZ DEFAULT now(), "
    "PRIMARY KEY(owner_id, group_key, target))"
)


async def _load_invited_targets(pool, owner_id: int, group_key: str) -> set:
    """Кого уже приглашали в эту группу. Fail-open: при сбое — пустой набор
    (лучше не дедупить, чем сорвать инвайт)."""
    try:
        await pool.execute(_INVITE_LOG_DDL)
        rows = await pool.fetch(
            "SELECT target FROM invite_target_log WHERE owner_id=$1 AND group_key=$2",
            owner_id, group_key,
        )
        # Ключи нормализованные: '@Ivan' и '@ivan' — один человек, и Telegram
        # матчит username именно так. Сырое сравнение строк давало второй инвайт
        # в ту же группу тому, кто попал в аудиторию из двух парсингов с разным
        # регистром (в реестре «не приглашать» это уже было учтено, здесь — нет).
        from services.contact_opt_out import compare_key as _ck
        return {_ck(r["target"]) for r in (rows or [])}
    except Exception:
        log_exc_swallow(log, "invite dedup: load failed")
        return set()


async def _record_invited_targets(pool, owner_id: int, group_key: str, op_id: int, targets) -> None:
    """Запомнить обработанные цели (best-effort, не роняет операцию)."""
    uniq = {str(t) for t in targets if t is not None}
    if not uniq:
        return
    try:
        await pool.execute(_INVITE_LOG_DDL)
        # Один запрос вместо N: прогон на 2000 успешных целей давал 2000
        # round-trip'ов в конце операции — минуты ожидания на ровном месте и
        # окно, в котором падение процесса теряло ВЕСЬ дедуп прогона.
        await pool.execute(
            "INSERT INTO invite_target_log(owner_id, group_key, target, op_id) "
            "SELECT $1, $2, t, $4 FROM unnest($3::text[]) AS t "
            "ON CONFLICT DO NOTHING",
            owner_id, group_key, sorted(uniq), op_id,
        )
    except Exception:
        log_exc_swallow(log, "invite dedup: record failed")


async def _exec_contacts_sync(
    pool: asyncpg.Pool, op_id: int, owner_id: int, params: dict
) -> dict:
    """Фоновая синхронизация контактов флота в единый хаб.

    Инлайн в HTTP-запросе 20+ аккаунтов не укладывались в таймаут шлюза и
    обрывались (клиент отключался → gather отменялся → в БД попадала лишь часть
    или НИЧЕГО). В фоне op_worker таймаута нет: sync_all_accounts проходит весь
    флот с внутренней конкуренцией и пишет контакты по мере обработки. Именно так
    «28 аккаунтов с контактами» перестают показывать пустой хаб.
    """
    from services.contacts_hub.sync_service import sync_all_accounts

    # Ограничение по времени: зависший на сети аккаунт не должен держать
    # единственный слот воркера бесконечно (уже записанные контакты сохранятся —
    # sync_account коммитит по контакту). 20 минут — с запасом на первичный полный
    # сбор большого флота (тысячи диалогов на аккаунт).
    try:
        res = await asyncio.wait_for(sync_all_accounts(pool, owner_id), timeout=1200)
    except asyncio.TimeoutError:
        await _safe_execute(
            pool, "UPDATE operation_queue SET done_items=total_items WHERE id=$1", op_id)
        return {"status": "done", "ok": 0, "failed": 0,
                "summary": "⏳ Синхронизация прервана по таймауту (20 мин) — часть "
                           "контактов сохранена. Запустите ещё раз, чтобы дособрать."}
    synced = int(res.get("total_synced", 0) or 0)
    created = int(res.get("total_created", 0) or 0)
    found = int(res.get("accounts_found", 0) or 0)
    errs = res.get("errors") or []
    await _safe_execute(
        pool, "UPDATE operation_queue SET total_items=$1, done_items=$1 WHERE id=$2",
        max(found, 1), op_id)
    if synced:
        summary = (f"✅ Синхронизировано контактов: {synced} (новых {created}) "
                   f"с {found} аккаунтов")
    else:
        # Честная причина из sync_all_accounts (различает системный сбой транспорта
        # и изолированные проблемы), а не гадание «устаревшие сессии».
        summary = "⚠️ " + (res.get("message") or f"Контакты не получены с {found} аккаунтов")
    return {"status": "done", "ok": synced, "failed": len(errs), "summary": summary}


# Явно небезопасные статусы аккаунта: инвайт с них = мгновенный бан или холостой
# ход. НЕ включаем 'active'/'cooldown'/'warming' и т.п. — свежие рабочие аккаунты
# отсевом НЕ трогаем (их бережёт cold-start дневного бюджета, а не блок).
_INVITE_UNSAFE_STATUS = {
    "spamblock", "banned", "deactivated", "archived", "session_expired", "no_session",
}


async def _filter_unready_for_invite(pool, op_id: int, accounts: list) -> list:
    """Отсеять аккаунты в ЯВНО небезопасном статусе. Fail-open.

    Раньше здесь стоял порог по trust (readiness>=0.50), но свежие аккаунты имеют
    trust_score=NULL→0.0 и отсекались даже с прокси (35+0+10=45<50) — для смешанного
    флота это вырезало свежее большинство и «флот не стартовал». Свежие аккаунты —
    ядро продукта; их безопасность держит cold-start бюджета, а не этот отсев.
    Здесь режем ТОЛЬКО заведомо мёртвые/забаненные/спамблок-аккаунты (по acc_status).
    _ACC_COLS не тянет acc_status → берём одним запросом. Пусто останется → fail-open."""
    if not accounts:
        return accounts
    ids = [int(a["id"]) for a in accounts]
    try:
        rows = await pool.fetch(
            "SELECT id, acc_status FROM tg_accounts WHERE id = ANY($1::bigint[])", ids)
    except Exception:
        return accounts  # нет сигналов → не режем
    meta = {int(r["id"]): (r["acc_status"] or "") for r in rows}
    kept = [a for a in accounts
            if str(meta.get(int(a["id"]), "")).strip().lower() not in _INVITE_UNSAFE_STATUS]
    if kept and len(kept) != len(accounts):
        log.info("mass_invite op=%d: %d аккаунтов в небезопасном статусе "
                 "(spamblock/banned/…) — пропущены", op_id, len(accounts) - len(kept))
        return kept
    return accounts  # все небезопасны → fail-open (не обнуляем операцию)


async def _exec_create_chatlist_folder(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Собрать общую папку из своих чатов и экспортировать chatlist-ссылку.

    Инвайтинг «от обратного»: одна ссылка добавляет человеку сразу всю связку
    каналов+чат. Ссылку экспортирует сессия аккаунта-владельца — поэтому это
    операция, а не синхронный вызов из API (баноопасность/FloodWait/Premium-гейт
    решаются здесь, в защищённом слое).
    """
    from services import account_manager, chatlist_folders as cf
    from database import db as _db

    folder_id = params.get("folder_id")
    title = params.get("title") or "Подборка"
    chat_ids = [int(c) for c in (params.get("chat_ids") or [])]
    acc_id = params.get("acc_id")
    if not chat_ids:
        return {"status": "failed", "summary": "⚠️ В папке нет чатов"}

    # Аккаунт-владелец сессии: либо указанный (загрузка одного по id), либо —
    # через единую флуд-осознанную дверь (resource_selector), а НЕ сырым SELECT
    # мимо неё: иначе можно взять аккаунт в кулдауне/на мёртвом прокси.
    if acc_id:
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, first_name, phone, username, device_model, "
            "system_version, app_version, lang_code, system_lang_code, cf_relay_url, "
            "proxy_id FROM tg_accounts WHERE id=$1 AND owner_id=$2 "
            "AND is_active=TRUE AND session_str IS NOT NULL", int(acc_id), owner_id)
    else:
        from services import resource_selector as _rs

        _cands = await _rs.select_all_active(pool, owner_id, action_type="default")
        acc = _cands[0] if _cands else None
    if not acc:
        return {"status": "failed", "summary": "⚠️ Нет активного аккаунта с сессией"}

    claimed = await try_claim_accounts([int(acc["id"])])
    if not claimed:
        return {"status": "requeue", "summary": "⚠️ Аккаунт занят другой операцией"}
    try:
        res = await account_manager.create_shared_folder_link(
            acc["session_str"], cf.clean_title(title), chat_ids, _acc=dict(acc))
    finally:
        await release_accounts([int(acc["id"])])

    if folder_id:
        try:
            await _db.set_chatlist_folder_result(pool, int(folder_id), owner_id, res)
        except Exception:
            log.warning("chatlist folder: не удалось записать итог folder=%s", folder_id)

    if not res.get("ok"):
        kind = res.get("error_kind")
        if kind == "premium":
            msg = ("⚠️ Для общих папок Telegram требует Premium на аккаунте, "
                   "которым создаётся папка. Включите Premium или выберите другой аккаунт.")
        elif kind == "peer":
            msg = "⚠️ Аккаунт не админ ни в одном из выбранных чатов — папку не собрать."
        elif kind == "flood":
            msg = f"⚠️ Telegram просит подождать: {res.get('error')}"
        elif kind == "auth":
            msg = "⚠️ Сессия аккаунта недействительна — переимпортируйте его."
        else:
            msg = f"⚠️ Не удалось создать папку: {res.get('error')}"
        return {"status": "failed", "summary": msg}

    return {"status": "done", "invite_link": res.get("invite_link"),
            "summary": f"📁 Папка готова — ссылка: {res.get('invite_link')}"}


async def _exec_mass_invite(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое добавление пользователей в группу.

    Распределяет user_refs и phones между аккаунтами равномерно.
    При PeerFlood у аккаунта — переключается на следующий.
    """
    from collections import deque

    from services import daughter_groups
    from services import showcase_layer
    from services import mass_inviter_engine as inv

    group = params.get("group", "")
    source = params.get("source", "")
    account_ids = [int(i) for i in (params.get("account_ids") or [])]
    user_refs: list[str | int] = list(params.get("user_refs") or [])
    phones: list[str] = list(params.get("phones") or [])
    batch_size: int = int(params.get("batch_size") or 5)
    # Темп: множитель паузы между батчами (инвайт — самая баноопасная операция).
    _pace = params.get("pace") or "normal"
    _pace_mult = {"slow": 2.5, "normal": 1.0, "fast": 0.5}.get(_pace, 1.0)
    _auto_reason = ""
    _batch_delay = 3.0 * _pace_mult
    # Максимум инвайтов за прогон (0 = без лимита) и лимит на аккаунт за прогон.
    try:
        _max_invites = max(0, int(params.get("max_invites") or 0))
    except (TypeError, ValueError):
        _max_invites = 0
    try:
        _per_acc_limit = max(0, int(params.get("per_account_limit") or 0))
    except (TypeError, ValueError):
        _per_acc_limit = 0
    # Режим «один проход»: осознанный выбор пользователя закрыть аудиторию за один
    # прогон, НЕ полагаясь на консервативный ПРЕДСКАЗАННЫЙ суточный лимит. Мы не
    # снимаем защиту целиком — потолок остаётся _INVITE_LIMIT_CEILING (за ним бан
    # почти гарантирован), а реальными тормозами становятся живые сигналы Telegram
    # (FloodWait/PeerFlood → аккаунт в cooldown) и стоп-кран флота (flood_storm).
    # Т.е. меняем ГАДАНИЕ о безопасном числе на реакцию по ФАКТУ, как у конкурентов.
    _one_pass = bool(params.get("one_pass"))
    # Режим объёма на аккаунт: "auto" (по истории) | "fixed" (one_pass+лимит) |
    # "progressive" (по возрасту/доверию аккаунта). Влияет на _acc_budget ниже.
    _volume_mode = str(params.get("volume_mode") or "").strip().lower()
    # Метод инвайта:
    #   "direct" — обычный InviteToChannelRequest (нужны права add_users);
    #   "admin"  — трюк через админку: цель промоутится в админы (это добавляет её
    #              в чат) → сразу снимаются все права → цель остаётся участником.
    #              Обходит приватность «кто может добавлять» и не требует, чтобы
    #              инвайтер имел право add_users — только право промоута (add_admins).
    #   "link"   — мягкий инвайт: рассылка ссылки-приглашения в ЛС; человек сам
    #              вступает. Полностью обходит приватность, наименее опасен, но не
    #              гарантирует вступление (ok = ссылка доставлена).
    _invite_method = str(params.get("invite_method") or "direct").strip().lower()
    if _invite_method not in ("direct", "admin", "link"):
        _invite_method = "direct"
    # Безопасный режим (governor уровня ЧАТА): дросселирует частоту системных
    # событий/мин по чату, замораживает приём при chat-flood, стопит на «мёртвом»
    # чате и серии выходов/жалоб. См. services/smart_invite.py.
    _safe_mode = str(params.get("safe_mode") or "").strip().lower() in ("1", "true", "yes", "on")
    # Пакетное добавление одним запросом (opt-in). Пер-операционный флаг поверх
    # глобального INVITE_BULK_API: обкатывать такое нужно на ЧАСТИ флота, а не
    # переключать сразу всем. None = взять значение из окружения.
    _bulk_api = params.get("bulk_api")
    _bulk_api = None if _bulk_api is None else bool(_bulk_api)
    # Необязательный текст сообщения для метода «ссылка в ЛС» ({link} — плейсхолдер).
    _link_msg = params.get("link_message") or None
    # «Мать-Дочка»: приглашать не в боевую (мать) группу, а в одноразовую дочернюю
    # с закреплённым редиректом на мать — риск бана инвайта поглощает расходник.
    # Если Telegram закрывает дочернюю группу, op_worker подставляет следующую и
    # продолжает прогон вместо полной остановки кампании (см. _rotate_daughter).
    _use_daughter = bool(params.get("use_daughter_groups"))
    # Витрина — буфер МЕЖДУ дочерней группой и боевым каналом. Без неё дочерняя
    # ведёт прямо в мать, и та получает всплеск вступлений по одной ссылке —
    # вектор, от которого «Мать-Дочка» не защищает. Осмысленна только вместе с
    # дочерними группами: сама по себе она лишь добавляет звено.
    _use_showcase = bool(params.get("use_showcase")) and _use_daughter

    if not group:
        return {"status": "failed", "summary": "⚠️ Не указана группа для инвайта"}

    # Последний рубеж: операция могла прийти не только из мини-аппа (бот, цепочка
    # продолжения, внешний API). Отказ ДО клейма аккаунтов и подключения — иначе
    # негодная ссылка сжигает прогон и суточные лимиты флота.
    _gok, _gwhy = inv.validate_group_ref(group, strict=False)
    if not _gok:
        return {"status": "failed",
                "summary": f"⚠️ {_gwhy}\n   Получено: {str(group)[:80]}"}

    if _pace == "auto":
        # Режим «авто»: темп берётся не из трёх чисел, выбранных вслепую, а из
        # состояния ВСЕГО флота за сегодня. Telegram смотрит на аккаунты как на
        # группу, поэтому и решение общее: флуд у одного тормозит всех.
        try:
            from services.flood_engine import auto_strategy
            _st = await auto_strategy(pool, owner_id)
            _pace_mult = float(_st.get("pace_mult") or 1.0)
            _batch_delay = 3.0 * _pace_mult
            if _st.get("batch_size"):
                batch_size = min(batch_size, int(_st["batch_size"]))
            _auto_reason = str(_st.get("reason") or "")
            log.info("mass_invite op=%d: авто-стратегия ×%.2f, батч %d (%s)",
                     op_id, _pace_mult, batch_size, _auto_reason)
        except Exception:
            # «Авто» не имеет права быть опаснее обычного режима: сбой расчёта →
            # базовый темп, а не самый быстрый.
            log.debug("mass_invite: auto strategy unavailable op=%d", op_id)
            _pace_mult, _batch_delay = 1.0, 3.0

    # ── Аудитория ────────────────────────────────────────────────────────────
    # Список либо передан явно (бот-хендлер шлёт user_refs/phones), либо задан
    # источником (мини-апп шлёт только {group, source}) — тогда грузим его здесь.
    #
    # ПОРЯДОК ВАЖЕН. Раньше источник читался одним `LIMIT 2000`, и только потом
    # применялись дедуп и реестр «не приглашать». Для второй кампании в ту же
    # группу это давало ложный тупик: свежие 2000 целиком состояли из уже
    # приглашённых, операция завершалась словами «все цели уже приглашались,
    # соберите свежую аудиторию» — при том, что в базе лежали десятки тысяч
    # НЕтронутых. Пользователь шёл собирать аудиторию, которая у него уже была.
    #
    # Теперь фильтры известны ДО чтения, а источник читается страницами, пока не
    # наберётся нужное число СВЕЖИХ целей (или источник не кончится). Усечение
    # больше не молчаливое: сколько отброшено и осталось — видно в итоге.
    _group_key = inv.parse_group_ref(group)
    _skip_invited = bool(params.get("skip_invited", True))
    _already = await _load_invited_targets(pool, owner_id, _group_key) if _skip_invited else set()
    from services import contact_opt_out as _coo
    _oo = await _coo.load_opted_out(pool, owner_id)
    _oo_keys = {_coo.compare_key(t) for t in _oo}
    _deduped = 0
    _opted_out = 0
    _source_exhausted = True     # источник вычитан до конца (не упёрлись в потолок)

    def _fresh(ref) -> bool:
        """Цель не приглашалась в эту группу и не в реестре «не приглашать».

        Сравнение регистронезависимо для username'ов: '@Ivan' и '@ivan' —
        один и тот же человек, и Telegram матчит их именно так. Раньше дедуп
        сравнивал строки как есть, поэтому один и тот же человек, попавший в
        аудиторию из двух парсингов с разным регистром, получал два инвайта в
        одну группу (opt-out это уже учитывал, дедуп — нет).
        """
        nonlocal _deduped, _opted_out
        key = _coo.compare_key(str(ref))
        if key in _oo_keys:
            _opted_out += 1
            return False
        if _skip_invited and key in _already:
            _deduped += 1
            return False
        return True

    if not user_refs and not phones:
        # Потолок пула целей за операцию. Прежние 2000 были и потолком, и
        # причиной ложного тупика выше; остаток всё равно уезжает в продолжение
        # (_schedule_invite_continuation), поэтому больший пул только помогает —
        # цепочка наконец получает, что дотягивать.
        _AUD_CAP = _INVITE_AUDIENCE_CAP
        _PAGE = 2000

        async def _page_source(sql: str, *args) -> list:
            """Читать источник страницами, оставляя только свежие цели.

            Возвращает список пар (ref, source_key) — вперемешку их разложит
            interleave_by_source. Останавливается, когда набрано _AUD_CAP свежих
            или источник кончился; факт упора в потолок фиксируется, чтобы итог
            не врал про «это вся аудитория».
            """
            nonlocal _source_exhausted
            out: list = []
            offset = 0
            while len(out) < _AUD_CAP:
                rows = await _safe_fetch(pool, sql + f" LIMIT {_PAGE} OFFSET {offset}", *args)
                if not rows:
                    return out
                offset += len(rows)
                for r in rows:
                    ref, skey = _row_to_ref(r)
                    if ref is None or not _fresh(ref):
                        continue
                    out.append((ref, skey))
                    if len(out) >= _AUD_CAP:
                        _source_exhausted = False
                        return out
                if len(rows) < _PAGE:
                    return out
            # Вышли по потолку пула, а не потому что источник кончился. Ветка
            # достижима, только если последняя страница дала ровно недостающее
            # число свежих целей, — но флаг честнее выставить здесь, чем
            # полагаться на то, что внутренняя проверка всегда сработает первой.
            _source_exhausted = False
            return out

        if source == "parsed":
            # Конкретный запуск парсера (мост «→ в инвайт»), иначе — вся аудитория.
            _pr = params.get("parse_run_id")
            try:
                _pr = int(_pr) if _pr else None
            except (TypeError, ValueError):
                _pr = None
            # Фильтры аудитории (только с username / не бот / premium / активные) —
            # те же, что в парсер-вью. Богатые колонки хранились, но инвайт их не
            # применял → в приглашение шли боты/удалённые/приватные без username.
            from services.audience_filters import parsed_audience_filters
            _af = params.get("aud_filters") or {}
            if _pr:
                _fsql, _fp = parsed_audience_filters(_af, base_params_count=2)
                _pairs = await _page_source(
                    "SELECT username, tg_user_id, source_username, source_id "
                    "FROM parsed_audiences "
                    f"WHERE owner_id=$1 AND parse_run_id=$2{_fsql} ORDER BY parsed_at DESC",
                    owner_id, _pr, *_fp)
            else:
                _fsql, _fp = parsed_audience_filters(_af, base_params_count=1)
                _pairs = await _page_source(
                    "SELECT username, tg_user_id, source_username, source_id "
                    "FROM parsed_audiences "
                    f"WHERE owner_id=$1{_fsql} ORDER BY parsed_at DESC",
                    owner_id, *_fp)
            # Источник каждой цели известен (из какого канала её спарсили) —
            # раскладываем вперемешку, чтобы аккаунт не приглашал подряд сорок
            # человек из одного канала. Подробности — в interleave_by_source.
            user_refs = interleave_by_source(_pairs)
        elif source == "crm":
            _pairs = await _page_source(
                "SELECT username, tg_user_id, phone FROM crm_contacts WHERE owner_id=$1 "
                "ORDER BY id DESC", owner_id)
            for ref, _ in _pairs:
                (phones if str(ref).startswith("+") else user_refs).append(ref)
        elif source == "segment":
            # Единый движок сегментов (unified_contacts): сохранённый срез или
            # фильтры — тот же таргетинг, что у рассылки.
            from services.contacts_hub import repository as _crepo
            _filters = None
            _ssid = params.get("saved_segment_id")
            if _ssid:
                _filters = await _crepo.get_segment_filters(pool, owner_id, int(_ssid))
            if _filters is None:
                _filters = params.get("segment_filters") or {}
            seg_rows = await _crepo.resolve_segment(pool, owner_id, _filters, limit=_AUD_CAP)
            if len(seg_rows) >= _AUD_CAP:
                _source_exhausted = False
            for c in seg_rows:
                u = (c.get("username") or "").lstrip("@")
                if u:
                    _ref = "@" + u
                elif c.get("telegram_user_id"):
                    _ref = c["telegram_user_id"]
                else:
                    _ph = c.get("phones")
                    _ref = str(_ph[0]) if isinstance(_ph, list) and _ph else None
                if _ref is None or not _fresh(_ref):
                    continue
                (phones if str(_ref).startswith("+") else user_refs).append(_ref)
        elif source == "bot_users":
            _pairs = await _page_source(
                "SELECT DISTINCT bu.user_id, bu.bot_id FROM bot_users bu "
                "JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
                "WHERE mb.added_by=$1 AND bu.is_active=TRUE ORDER BY bu.user_id",
                owner_id)
            # Разные боты — разные источники аудитории, раскладываем так же.
            user_refs = interleave_by_source(_pairs)
    else:
        # Список передан явно (бот-хендлер): те же два фильтра, тот же
        # регистронезависимый ключ — иначе поведение зависело бы от поверхности.
        user_refs = [r for r in user_refs if _fresh(r)]
        phones = [p for p in phones if _fresh(p)]

    if not user_refs and not phones:
        return {
            "status": "failed",
            "summary": f"⚠️ Аудитория пуста — нечего добавлять (источник: {source or 'не задан'})",
        } if not (_deduped or _opted_out) else {
            "status": "done", "ok": 0, "failed": 0, "left": 0,
            "summary": (
                (f"🚫 Все цели ({_opted_out}) в реестре «не приглашать» — новых нет."
                 if _opted_out and not _deduped else
                 f"♻️ Новых целей нет: уже приглашались в эту группу — {_deduped}"
                 + (f", в реестре «не приглашать» — {_opted_out}" if _opted_out else "")
                 + ". Соберите свежую аудиторию или отключите дедуп.")),
        }

    # account_ids is optional in the Mini App ("не выбрано = все активные"):
    # fall back to every active account that still has a usable session.
    if not account_ids:
        acc_rows = await _safe_fetch(
                pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "AND session_str IS NOT NULL "
            "AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired')",
            owner_id,
        )
        account_ids = [r["id"] for r in acc_rows]

    if not account_ids:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов-инвайтеров с сессией"}

    # Keep op progress meaningful: total_items reflects the real audience size
    # (the Mini App submits total_items=1 as a placeholder).
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2",
        len(user_refs) + len(phones), op_id,
    )

    # Владелец может ПРИНУДИТЕЛЬНО взять рисковые аккаунты («если очень нужно»):
    # include_risky снимает МЯГКИЕ гейты — кулдаун и карантин риск-пульса. Жёсткие
    # (banned/spamblock/session_expired, мёртвый прокси) остаются: забаненный
    # аккаунт всё равно не пригласит, форсить его — только жечь. По умолчанию OFF.
    _include_risky = bool(params.get("include_risky"))
    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids],
        min_trust_score=0.0, respect_cooldown=not _include_risky)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Учёт задействования аккаунтов — чтобы честно объяснить «выбрал 28, работали 20»
    # (класс багов #4: молчаливый итог). _selected — сколько отобрано; _quarantined_n —
    # сколько снято риск-пульсом; _used_accounts заполняется в цикле.
    _selected_n = len(accounts)
    _quarantined_n = 0
    _used_accounts: set[int] = set()
    _daily_capped: set[int] = set()
    _noconnect_errs: list[str] = []   # тексты ошибок «не подключился» — для распознавания причины

    # Риск-пульс (Волна S/1B + M, fail-open): инвайт с флагнутого аккаунта = быстрый
    # бан. Отсеиваем карантинные; пустой результат НЕ обнуляет операцию.
    # include_risky (владелец форсит рисковых) отключает этот отсев осознанно.
    _risky_forced = 0
    if _include_risky:
        # Считаем, сколько форсированных было бы отсеяно — для честного итога.
        try:
            for a in accounts:
                if await _infra_mem.is_account_quarantined(pool, a["id"]):
                    _risky_forced += 1
        except Exception:
            log_exc_swallow(log, f"mass_invite op={op_id}: risky count failed")
        if _risky_forced:
            log.info("mass_invite op=%d: включены ПРИНУДИТЕЛЬНО %d рисковых аккаунтов "
                     "(include_risky) — карантин и кулдаун сняты владельцем",
                     op_id, _risky_forced)
    else:
        try:
            _kept = [a for a in accounts
                     if not await _infra_mem.is_account_quarantined(pool, a["id"])]
            if _kept and len(_kept) != len(accounts):
                _quarantined_n = len(accounts) - len(_kept)
                log.info("mass_invite op=%d: пропущено %d аккаунтов в карантине",
                         op_id, _quarantined_n)
                accounts = _kept
        except Exception:
            log_exc_swallow(log, f"mass_invite op={op_id}: quarantine check failed")

    # Гейт готовности (fail-open): не инвайтить с неготовых аккаунтов — низкий
    # trust / нет прокси / spamblock / cooldown = инвайт с них = быстрый бан
    # молодого/слабого аккаунта. Порог min_trust_for_action('invite')=0.50.
    # Дополняет карантин (тот ловит недавние ОГРАНИЧЕНИЯ, этот — общую слабость).
    accounts = await _filter_unready_for_invite(pool, op_id, accounts)

    # Клейм аккаунтов под эту операцию — защита от AUTH_KEY_DUPLICATED («сессия
    # использовалась с двух IP одновременно»): берём атомарно только СВОБОДНЫЕ (не
    # занятые прогревом/другой операцией) и помечаем занятыми, чтобы никто не
    # подключил ту же сессию параллельно = мгновенный бан ключа. Инвайт был
    # единственным массовым исполнителем БЕЗ клейма (в отличие от strike/warmup/
    # publish). Освобождение — автоматически в finally _run_op_task
    # (release_operation_accounts по op_id), утечки claim нет.
    _busy_skipped = 0
    try:
        _free = await _claim_available_accounts(op_id, accounts, owner_id)
        _busy_skipped = len(accounts) - len(_free)
        if _busy_skipped:
            log.info("mass_invite op=%d: %d аккаунтов заняты другой операцией — пропущены",
                     op_id, _busy_skipped)
        accounts = _free
    except Exception:
        log_exc_swallow(log, f"mass_invite op={op_id}: claim accounts failed")

    if not accounts:
        # requeue, а не done/failed: раньше отправляло владельца перезапускать
        # операцию вручную — теперь живая очередь (см. _requeue_op_no_accounts)
        # сама стартует прогон, когда флот освободится, до 20 минут.
        return {
            "status": "requeue",
            "summary": ("⏸ Все подходящие аккаунты сейчас заняты другими операциями "
                        "(прогрев/страйк/другой инвайт) — операция в очереди, стартует "
                        "автоматически, когда флот освободится."),
        }

    # ── «Мать-Дочка»: подменяем group дочерней, пока она жива ──────────────────
    _mother_ref = group if _use_daughter else ""
    _daughter_id: int | None = None
    _daughter_rotations = 0
    _showcase_id: int | None = None
    _showcase_ref = ""
    if _use_showcase:
        _sc = await showcase_layer.get_or_create(
            pool, owner_id, _mother_ref, dict(accounts[0]))
        if _sc.get("ok"):
            _showcase_id = _sc["id"]
            _showcase_ref = _sc["showcase_ref"]
            log.info("mass_invite op=%d: включена витрина — дочерняя ведёт в буфер, "
                     "а не в боевой канал", op_id)
        else:
            # Витрина не поднялась — это не повод ронять прогон, но и молчать
            # нельзя: владелец рассчитывал на защиту от всплеска, а её нет.
            log.warning("mass_invite op=%d: витрина не создана (%s) — дочерняя "
                        "ведёт прямо в мать", op_id, _sc.get("error"))
            _use_showcase = False
    if _use_daughter:
        _dg = await daughter_groups.get_or_create_active(
            pool, owner_id, _mother_ref, dict(accounts[0]), redirect_ref=_showcase_ref)
        if not _dg.get("ok"):
            return {"status": "failed",
                    "summary": f"⚠️ Не удалось подготовить дочернюю группу: {_dg.get('error')}"}
        group = _dg["group_ref"]
        _daughter_id = _dg["id"]
        log.info("mass_invite op=%d: инвайт идёт в дочернюю группу (мать %s)",
                 op_id, _mother_ref[:80])

    async def _rotate_daughter(reason: str) -> bool:
        """Дочерняя группа сгорела: пробуем следующую вместо остановки прогона.

        True — group заменена, можно продолжать текущий прогон. False — не в
        режиме дочерних групп, лимит ротаций исчерпан или создание не вышло;
        вызывающий код останавливает прогон как раньше (group_broken).
        """
        nonlocal group, _daughter_id, _daughter_rotations
        if not _use_daughter or _daughter_id is None:
            return False
        if _daughter_rotations >= daughter_groups.MAX_ROTATIONS_PER_RUN:
            log.warning("mass_invite op=%d: лимит ротаций дочерних групп исчерпан (%d)",
                        op_id, daughter_groups.MAX_ROTATIONS_PER_RUN)
            return False
        await daughter_groups.mark_burned(pool, _daughter_id, reason)
        _creator = next((a for a in accounts if int(a["id"]) not in retired), accounts[0])
        _dg = await daughter_groups.get_or_create_active(
            pool, owner_id, _mother_ref, dict(_creator), redirect_ref=_showcase_ref)
        if not _dg.get("ok"):
            log.warning("mass_invite op=%d: следующую дочернюю группу создать не удалось: %s",
                        op_id, _dg.get("error"))
            return False
        _daughter_rotations += 1
        group = _dg["group_ref"]
        _daughter_id = _dg["id"]
        log.info("mass_invite op=%d: дочерняя группа сожжена (%s) — переключились на новую",
                 op_id, reason[:120])
        return True

    async def _rest_invite_account(acc_id: int, res: dict) -> None:
        """Дать флагнутому/зафлуженному инвайт-аккаунту cooldown через ЕДИНЫЙ
        flood-сигнал. Без этого переключение внутри операции недостаточно: флаг
        живёт на аккаунте, и следующая операция сразу его добьёт (anti-detection).
        PeerFlood → 48ч; длинный FloodWait → ровно на длительность флуда."""
        try:
            from services.flood_engine import record_peer_flood, record_flood
            if res.get("peer_flood"):
                await record_peer_flood(pool, acc_id, "invite", op_id)
            elif res.get("flood_wait"):
                await record_flood(pool, acc_id, wait_seconds=int(res["flood_wait"]),
                                   action_type="invite", operation_id=op_id)
        except Exception:
            log_exc_swallow(log, "mass_invite: rest-account cooldown failed")

    def _invite_pause(acc_id: int) -> float:
        """Адаптивная пауза перед следующим батчем ЭТОГО аккаунта.

        Раньше пауза была фиксированной: 3с × множитель режима, одинаковая для
        всех аккаунтов и не меняющаяся от их состояния. Это ровно то, что делают
        конкуренты, и это палевно: ровный интервал у десятков аккаунтов — сам по
        себе координационный признак.

        Теперь темп считает `flood_engine.recommended_delay(acc, "invite")`,
        который уже учитывает: базовую ставку действия, ВЫУЧЕННУЮ поправку по
        прошлым флудам этого аккаунта, хвост активного cooldown, затухание
        штрафа со временем и глобальный флотовый темп (pacing_engine). Поверх
        накладывается `gaussian_delay` — интервалы получаются неровными
        (18/31/24/42…), как у живого человека, а не по метроному.

        Режим пользователя (slow/normal/fast) остаётся СМЕЩЕНИЕМ поверх расчёта,
        а не заменой: «быстро» ускоряет относительно безопасного темпа, но не
        отменяет ни cooldown, ни выученный штраф.
        Сбой движка → прежняя фиксированная пауза (темп важнее, чем упасть).
        """
        try:
            from services import flood_engine as _fe
            base = _fe.recommended_delay(int(acc_id), "invite")
            base = max(1.0, base * _pace_mult)
            return _fe.gaussian_delay(base, spread=0.22, minimum=1.0, maximum=900.0)
        except Exception:
            log.debug("mass_invite: adaptive pause unavailable, using fixed")
            return _batch_delay

    async def _humanize(acc: dict) -> None:
        """Разбавить след аккаунта человеческим действием между батчами.

        Адаптивная пауза делает РИТМ похожим на человеческий, но не меняет
        состава следа: аккаунт, у которого в API только InviteToChannel × N,
        отделяется от живого тривиально — по набору вызовов, а не по их темпу.
        Поэтому в паузу иногда вплетается обычное действие клиента (онлайн,
        список диалогов, чтение своего диалога).

        Сбой здесь не имеет права влиять на инвайт: это улучшение следа, а не
        обязательство. Модуль сам ничего не бросает, но мы страхуемся и тут.
        """
        try:
            from services import invite_behavior
            done = await invite_behavior.humanize(dict(acc))
            if done:
                log.debug("mass_invite op=%d acc=%s: вплетено действие %s",
                          op_id, acc.get("id"), done)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("mass_invite: humanize failed acc=%s", acc.get("id"))

    async def _invite_learn_ok(acc_id: int, ok_count: int) -> None:
        """Замкнуть петлю обучения: успешный батч снижает риск-штраф аккаунта.

        Без этого система умела только НАКАЗЫВАТЬ (record_flood), но никогда не
        реабилитировала: аккаунт, однажды словивший флуд, навсегда оставался
        «медленным», даже отработав сотни инвайтов без единой проблемы.
        """
        if ok_count <= 0:
            return
        try:
            from services.flood_engine import record_success
            await record_success(int(acc_id), "invite")
        except Exception:
            log.debug("mass_invite: record_success failed acc=%s", acc_id)

    total_ok, total_fail = 0, 0
    all_users = list(user_refs)
    all_phones = list(phones)

    # ── Продвинутый режим: автовыдача админки инвайтерам + промоут-трюк ────────
    # Инвайт в КАНАЛ вообще невозможен без прав админа у инвайтера, а часть целей
    # блокирует прямой инвайт приватностью. Поэтому: (1) находим аккаунт-создателя/
    # админа чата и выдаём остальным инвайтерам право invite_users; (2) цели,
    # отклонённые приватностью, добавляем «промоут-трюком» (выдать/снять админку)
    # аккаунтом с правом add_admins. Оба шага опциональны и fail-open.
    _promoter = None            # (acc_dict) — аккаунт с правами выдавать админку
    _privacy_blocked: list = []  # цели, отклонённые приватностью (для промоут-трюка)
    _promoted_n = 0
    # Ротация мест админа: при большом флоте у чата не хватает мест на всех
    # разом (Telegram: CHAT_ADMINS_TOO_MUCH) — аккаунты, отработавшие свой
    # батч, обязаны отдавать место следующим в очереди, а не держать права
    # админа до конца прогона впустую. _admin_seats — кто СЕЙЧАС держит место
    # (наполняется при каждом успешном промоуте — и в первичной раздаче, и «на
    # лету»); _admin_seats_freed — счётчик для честного итога.
    _admin_seats: set[int] = set()
    _admin_seats_freed = 0
    _promote_failed = 0          # инвайтеры, кому промоут вернул False/ошибку (не участник/нет прав)
    _promote_no_uid = 0          # инвайтеры, чей user_id не удалось получить (промоут пропущен)
    _promote_skipped_no_admin = False  # ни один аккаунт не админ чата → некому выдать право
    _acc_uid: dict = {}          # id аккаунта → tg_user_id (для промоута; наполняется ниже)
    # Сколько раз пробовали выдать права каждому инвайтеру по ходу прогона.
    # Было множеством («уже пробовали») — одна осечка по сети/флуду промоутера
    # стоила аккаунта на весь прогон (в отчёте: «N аккаунтов без прав выведены
    # из круга»). Считаем попытки, потолок — против зацикливания.
    #
    # Потолка мало: повторять надо ТОЛЬКО временные отказы. Самый частый —
    # not_participant: Telegram не успевает зарегистрировать вступление, и
    # промоутер честно отвечает «не участник». Отказ «у промоутера нет права
    # add_admins» окончательный, попытки на него тратить нельзя. Причину даёт
    # promote_to_admin_ex.
    from services.invite_recovery import (
        promote_retry_allowed as _irec_promote_retry_allowed,
        MAX_ADMIN_SEAT_WAIT_ATTEMPTS as _MAX_ADMIN_SEAT_WAIT_ATTEMPTS,
    )
    _no_rights_on_demand: dict = {}
    _auto_promote = params.get("auto_promote", True)
    _promote_trick = params.get("promote_trick", True)
    # Метод «ссылка в ЛС» вообще не требует прав админа — человек вступает сам.
    # Поэтому вся возня с промоутером/автовыдачей админки для него пропускается.
    if _invite_method == "link":
        _auto_promote = False
        _promote_trick = False
    if (_auto_promote or _promote_trick) and accounts:
        from services import mass_inviter_engine as _inv
        from services import account_manager
        # tg_user_id инвайтеров — кого промоутить (в _ACC_COLS их нет).
        _uid_rows = await _safe_fetch(
            pool, "SELECT id, tg_user_id FROM tg_accounts WHERE owner_id=$1 AND id=ANY($2::bigint[])",
            owner_id, [int(a["id"]) for a in accounts])
        _acc_uid = {int(r["id"]): r["tg_user_id"] for r in (_uid_rows or []) if r.get("tg_user_id")}
        # Ищем промоутера: первый аккаунт-создатель или админ с add_admins.
        # _admin_probe_ok: хоть один аккаунт РЕАЛЬНО подключился и ответил о правах.
        # Без этого нельзя утверждать «админа нет» — возможно, флот просто не
        # подключился (тогда причина в сессии/сети, а не в отсутствии прав).
        _admin_probe_ok = False
        for a in accounts:
            try:
                st = await asyncio.wait_for(
                    _inv.channel_admin_status(a["session_str"], dict(a), group),
                    timeout=45)
            except Exception:
                continue
            if st.get("ok"):
                _admin_probe_ok = True
                if st.get("can_promote"):
                    _promoter = a
                    break
        if _promoter is None and _admin_probe_ok:
            # Аккаунты подключились и ответили — но ни у кого нет права промоута.
            _promote_skipped_no_admin = bool(_auto_promote)
            await _safe_execute(
                pool, "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,0,'promote','skip',$2)", op_id,
                "автовыдача админки пропущена: ни один инвайтер не создатель/админ чата "
                "с правом «Назначать админов». Сделайте один аккаунт админом чата.")
        elif _promoter is None:
            # Ни один аккаунт не подключился → проверить права было невозможно.
            # НЕ утверждаем «админа нет» (это ввело бы в заблуждение — как в жалобе,
            # где админ был, но флот не подключился). Причину покажет блок сессии/сети.
            await _safe_execute(
                pool, "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,0,'promote','skip',$2)", op_id,
                "проверка прав админа не выполнена: ни один аккаунт не подключился "
                "(см. причину «сессия/сеть»). Права admin могли быть — но проверить их "
                "без подключения нельзя.")
        elif _auto_promote:
            # Потолок мест админа у ЭТОГО чата ещё не известен (узнаём только
            # по факту первого отказа Telegram, см. admins_too_much) — пока не
            # уткнулись в него, раздаём права как раньше, всем подряд.
            _admin_cap_hit = False
            for a in accounts:
                if int(a["id"]) == int(_promoter["id"]):
                    continue
                if _admin_cap_hit:
                    # Дальше пытаться бессмысленно — И дорого: join+promote на
                    # каждый гарантированно неудачный аккаунт стоит секунды
                    # впустую при большом флоте (сотни аккаунтов сверх лимита
                    # чата — это были бы лишние минуты работы ради заведомого
                    # отказа). Остаток получит права «на лету» — по очереди, по
                    # мере того как ротация освобождает места (см. ниже,
                    # ветка res.get("no_rights") и "admins_too_much").
                    break
                _u = _acc_uid.get(int(a["id"]))
                if not _u:
                    # tg_user_id не заполнен (частый случай при импорте сессий) —
                    # промоутить по id некого. Раньше аккаунт молча пропускался, и
                    # автовыдача админки тихо не срабатывала → инвайтер без прав.
                    # Резолвим id вживую по его же сессии, сохраняем и используем.
                    try:
                        _u = await asyncio.wait_for(
                            account_manager.resolve_self_user_id(a["session_str"], dict(a)),
                            timeout=30)
                    except Exception:
                        _u = None
                    if _u:
                        _acc_uid[int(a["id"])] = int(_u)
                        await _safe_execute(
                            pool, "UPDATE tg_accounts SET tg_user_id=$2 "
                            "WHERE id=$1 AND (tg_user_id IS NULL OR tg_user_id=0)",
                            int(a["id"]), int(_u))
                if not _u:
                    _promote_no_uid += 1
                    continue
                # КЛЮЧЕВОЕ: promote_to_admin требует, чтобы инвайтер УЖЕ БЫЛ
                # участником чата (иначе UserNotParticipantError → False). Для
                # публичного @канала аккаунт сам не вступает (в отличие от
                # приват-ссылки, где ImportChatInvite вступает). Без этого шага
                # админка не выдавалась и инвайтил только создатель. Присоединяем
                # инвайтера к цели ДО выдачи прав (best-effort; уже-участник = ok).
                try:
                    await asyncio.wait_for(
                        account_manager.join_channel(a["session_str"], group, _acc=dict(a)),
                        timeout=45)
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                except Exception:
                    log.debug("mass_invite op=%d: pre-promote join acc=%s failed",
                              op_id, a.get("id"))
                try:
                    okp, _pr_reason = await asyncio.wait_for(
                        account_manager.promote_to_admin_ex(
                            _promoter["session_str"], group, int(_u),
                            _acc=dict(_promoter), invite_users=True, post_messages=False,
                            # Метод «admin»: инвайтер сам добавляет цели промоут-трюком,
                            # значит ему нужно право «Назначать админов» (add_admins),
                            # а не только invite_users.
                            add_admins=(_invite_method == "admin")),
                        timeout=45)
                    if okp:
                        _promoted_n += 1
                        _admin_seats.add(int(a["id"]))
                    else:
                        _promote_failed += 1
                        if _pr_reason == "admins_too_much":
                            _admin_cap_hit = True
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                except Exception as _pe:
                    _promote_failed += 1
                    log.debug("mass_invite op=%d: promote acc=%s failed: %s", op_id, a.get("id"), _pe)
            _rights_word = "invite_users+add_admins" if _invite_method == "admin" else "invite_users"
            _promote_msg = f"выдана админка ({_rights_word}) {_promoted_n} инвайтерам через аккаунт #{_promoter['id']}"
            if _promote_no_uid:
                _promote_msg += (f"; {_promote_no_uid} пропущено — не удалось получить их user_id "
                                 "(переавторизуйте эти аккаунты)")
            if _promote_failed:
                _promote_msg += (f"; {_promote_failed} не выдана — промоутер без права "
                                 "«Назначать администраторов» или аккаунт не вступил в чат")
            if _admin_cap_hit:
                _promote_msg += (f"; у чата закончились места админа ({len(_admin_seats)}) — "
                                 "остальные аккаунты получат права по очереди, по мере того как "
                                 "отработавшие свой батч их освобождают")
            await _safe_execute(
                pool, "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,0,'promote','ok',$2)", op_id, _promote_msg)

    async def _release_admin_seat(acc_id: int) -> None:
        """Снять права админа с аккаунта, если он их держал, — освобождает
        место для следующего в очереди ротации (см. "admins_too_much" выше).

        Вызывается при КАЖДОМ выводе аккаунта из круга (retired.add): аккаунт,
        закончивший работу (батч/лимит/флуд/сбой), больше не нуждается в
        админке, и держать её впустую — и лишний след для детекта, и напрасно
        занятое место, если у чата их не хватает на весь флот. Best-effort:
        сбой демоута не должен ронять прогон — аккаунт просто останется
        админом чуть дольше."""
        nonlocal _admin_seats_freed
        if acc_id not in _admin_seats or _promoter is None:
            return
        _u = _acc_uid.get(acc_id)
        if not _u:
            _admin_seats.discard(acc_id)  # без user_id снять права нечем — просто забываем о месте
            return
        try:
            ok = await asyncio.wait_for(
                account_manager.demote_from_admin(
                    _promoter["session_str"], group, int(_u), _acc=dict(_promoter)),
                timeout=30)
            if ok:
                _admin_seats_freed += 1
        except Exception as _de:
            log.debug("mass_invite op=%d acc=%s: demote_from_admin failed: %s",
                      op_id, acc_id, _de)
        _admin_seats.discard(acc_id)

    # ── Очередь-планировщик ──────────────────────────────────────────────────
    # Раньше аудитория НАРЕЗАЛАСЬ по аккаунтам заранее (_chunks). Цену платил
    # пользователь, причём дважды:
    #   • аккаунт словил флуд на середине своего куска — весь его хвост исчезал
    #     НАВСЕГДА, хотя рядом стояли свежие аккаунты;
    #   • аккаунт, чей кусок кончился или был срезан суточным лимитом, просто
    #     простаивал, пока другие догрызали свои куски.
    # Теперь цели лежат в ОДНОЙ общей очереди, аккаунты разбирают её по батчу за
    # круг, а неотработанный хвост возвращается в голову очереди. Побочный — и
    # для anti-detection не менее важный — эффект: круговая ротация вместо
    # «выжать один аккаунт досуха, потом взяться за следующий» размазывает
    # нагрузку по флоту, а не рисует всплеск на одном аккаунте.
    q_users: deque = deque(all_users)
    q_phones: deque = deque(all_phones)
    retired: set[int] = set()     # аккаунты, выбывшие из круга (флуд/лимит/сбой)
    _no_rights_retired = 0        # аккаунтов выведено из-за отсутствия прав админа
    budget: dict[int, int] = {}   # остаток инвайтов на аккаунт за прогон
    group_broken = False          # группа недоступна — гнать по ней флот бессмысленно
    _group_broken_reason = ""     # почему остановились по чату (safe-режим/недоступность)

    # Безопасный режим: ДО массового притока оцениваем живость чата (участники +
    # свежесть переписки). «Холодный» приток в тихий/малолюдный чат мгновенно
    # ловит флуд, поэтому governor режет темп/останавливает по liveness. Best-
    # effort одним аккаунтом; сбой не рушит операцию (governor работает и без оценки).
    if _safe_mode and accounts:
        try:
            from services import smart_invite as _si
            _lv_acc = dict(accounts[0])
            _lv_client = await account_manager.connect_client(
                _lv_acc["session_str"], _lv_acc, "resolve")
            try:
                _lv_entity = await inv._resolve_group_entity(
                    _lv_client, group, acc_id=_lv_acc.get("id"))
                _lv_score = await _si.assess_and_store_liveness(
                    pool, owner_id, _group_key, _lv_client, _lv_entity)
                log.info("mass_invite op=%d: safe-режим liveness чата = %s", op_id, _lv_score)
            finally:
                try:
                    await _lv_client.disconnect()
                except Exception:
                    pass
        except Exception as _lv_exc:
            log.debug("mass_invite op=%d: liveness-оценка пропущена: %s", op_id, _lv_exc)
    _group_reason = ""            # ЧЕМ именно недоступна (нет прав/закрыта/лимит)
    _fail_reasons: dict[str, int] = {}  # причина отказа → счётчик (для честного итога)
    # Стоп-кран по флудам на ВЕСЬ флот: Telegram смотрит на аккаунты как на группу,
    # поэтому N подряд PeerFlood/FloodWait без единого успеха = флот перегрет,
    # продолжать значит жечь оставшиеся аккаунты в бан. Порог из env (0 = выкл).
    flood_storm = False
    flood_streak = 0
    invited_this_run: set = set()  # цели, реально отданные движку — для дедупа впредь
    _phones_not_found = 0          # номеров не в Telegram (для честного отчёта)
    _ok_logged = 0                 # сколько успешных целей записано в лог (для CSV)
    _ok_seen: set = set()          # уже залогированные 'ok' цели (без дублей в отчёте)
    _OK_LOG_CAP = 20000            # потолок per-target 'ok' строк, чтобы не пух лог
    import os as _os_env
    try:
        _flood_stop_streak = max(0, int(_os_env.getenv("INVITE_FLOOD_STOP_STREAK", "5")))
    except (TypeError, ValueError):
        _flood_stop_streak = 5
    # Окно шторма. Прежний счётчик считал не «флуды подряд по времени», а
    # «батчи с флудом, между которыми не было НИ ОДНОГО успешного батча», и
    # сбрасывался только при ok>0. К концу прогона в очереди остаются в основном
    # цели с закрытой приватностью: батч честно отдаёт ok=0, серию не сбрасывает,
    # и пять флудов, разбросанных по двум часам, складывались в «шторм»,
    # которого нет. Так остановился прогон, где на 380 целей было 6 флудов (1,6%)
    # и 203 успеха — причём в режиме «один проход», где стоп-кран и есть
    # единственная защита вместо суточного лимита.
    #
    # Настоящий шторм — это КУЧНОСТЬ: Telegram смотрит на флот как на группу и
    # бьёт по нему в короткий промежуток. Поэтому считаем флуды в скользящем
    # окне времени.
    try:
        _flood_window_sec = max(60, int(_os_env.getenv("INVITE_FLOOD_WINDOW_SEC", "600")))
    except (TypeError, ValueError):
        _flood_window_sec = 600
    _flood_times: list = []        # отметки времени флудов (монотонные часы)
    # ДЛИННЫЙ FloodWait (≥ порога) — это НЕ «притормози», а «аккаунт достиг
    # жёсткого потолка приглашений и забанен на приглашения на ~сутки». Считать
    # его наравне с 60-секундным (обычный стоп-кран) — значит позволить флоту
    # выжечь себя в суточные баны по одному (реальный лог: 7 аккаунтов по 86400с,
    # а операция всё шла). Поэтому длинные флуды считаем ОТДЕЛЬНО и стопаем раньше:
    # несколько суточных банов = весь флот у потолка по этому чату/паттерну.
    try:
        _long_flood_s = max(300, int(_os_env.getenv("INVITE_LONG_FLOOD_SEC", "3600")))
    except (TypeError, ValueError):
        _long_flood_s = 3600
    try:
        _long_flood_stop = max(0, int(_os_env.getenv("INVITE_LONG_FLOOD_STOP", "3")))
    except (TypeError, ValueError):
        _long_flood_stop = 3
    _long_floods = 0               # сколько аккаунтов получили ДЛИННЫЙ (суточный) флуд
    # Сколько аккаунтов флота инвайтят ОДНОВРЕМЕННО. Раньше круг обрабатывал
    # аккаунты строго по одному (`for acc in active` с await на каждый батч):
    # флот из 20 слал батчи по очереди, пользователь видел «работает один
    # аккаунт», а при короткой аудитории до дальних аккаунтов очередь не
    # доходила вовсе. Теперь батчи круга уходят в сеть параллельно, чанками по
    # _parallel; учёт (счётчики/флуд/дедуп/права/лог) остаётся ПОСЛЕДОВАТЕЛЬНЫМ.
    # Разные аккаунты — разные сессии/прокси, для Telegram это независимые
    # клиенты; параллельность НЕ ускоряет отдельный аккаунт, лишь убирает
    # простой флота. INVITE_PARALLEL=1 возвращает прежнее строго последовательное
    # поведение (аварийный откат без передеплоя).
    try:
        _parallel = max(1, int(_os_env.getenv("INVITE_PARALLEL", "4")))
    except (TypeError, ValueError):
        _parallel = 4

    def _take(q: deque, n: int) -> list:
        out: list = []
        while q and len(out) < n:
            out.append(q.popleft())
        return out

    def _give_back(q: deque, items: list) -> None:
        """Вернуть неотработанное в ГОЛОВУ очереди — порядок аудитории сохраняется."""
        for it in reversed(items):
            q.appendleft(it)

    async def _acc_budget(acc) -> int:
        """Сколько инвайтов аккаунт вправе сделать за прогон (0 = выбыл).

        Берём СТРОЖАЙШИЙ из двух — заданного пользователем и рекомендованного по
        фактической истории самого аккаунта. Ручное число одно на всю операцию и
        не знает, что один аккаунт год работает чисто, а другой словил флуд
        вчера; рекомендация это знает. Уже израсходованное за сегодня
        вычитается, поэтому повторный запуск не удваивает суточный объём.
        """
        # Режим «прогрессивно»: лимит на аккаунт по его возрасту/доверию (свежим
        # мало, отстоявшимся больше). Вычитаем сделанное сегодня — повторный запуск
        # не удваивает объём. Живые сигналы флуда всё так же тормозят.
        if _volume_mode == "progressive":
            try:
                from services.flood_engine import progressive_daily_cap
                _pg = await progressive_daily_cap(pool, int(acc["id"]))
                return max(0, int(_pg.get("remaining") or 0))
            except Exception:
                log.debug("mass_invite: progressive cap unavailable acc=%s", acc.get("id"))
                return 5
        # Режим «один проход»: не клампим к предсказанному суточному лимиту —
        # берём потолок ёмкости (или явный лимит пользователя, но не выше потолка).
        # Реальную безопасность держат сигналы флуда + стоп-кран флота, а не гадание.
        if _one_pass:
            from services.flood_engine import _INVITE_LIMIT_CEILING as _ceil
            _cap = _per_acc_limit or _ceil
            return max(1, min(int(_cap), _ceil))
        _acc_cap = _per_acc_limit
        try:
            from services.flood_engine import recommended_daily_limit
            _rec = await recommended_daily_limit(pool, int(acc["id"]))
            _remaining = int(_rec.get("remaining") or 0)
            _acc_cap = _remaining if not _acc_cap else min(_acc_cap, _remaining)
            if _remaining <= 0:
                log.info(
                    "mass_invite op=%d acc=%s: суточный лимит исчерпан (%s/%s, %s)",
                    op_id, acc.get("id"), _rec.get("used_today"), _rec.get("limit"),
                    _rec.get("basis"),
                )
                return 0
        except Exception:
            log.debug("mass_invite: daily limit unavailable acc=%s", acc.get("id"))
        # Ни ручного лимита, ни рекомендации — потолком служит сама очередь.
        return _acc_cap if _acc_cap else (len(all_users) + len(all_phones))

    # Метод «ссылка в ЛС»: экспортируем ссылку-приглашение ОДИН раз (первым
    # аккаунтом, у которого получилось) — её будут рассылать все инвайтеры.
    _invite_link = ""
    if _invite_method == "link":
        from services import mass_inviter_engine as _lnk
        for a in accounts:
            try:
                _invite_link = await asyncio.wait_for(
                    _lnk.export_group_invite_link(a["session_str"], dict(a), group),
                    timeout=45)
            except Exception:
                _invite_link = ""
            if _invite_link:
                break
        if not _invite_link:
            return {"status": "failed",
                    "summary": "⚠️ Не удалось получить ссылку-приглашение группы. "
                    "Нужен аккаунт с правом «Пригласительные ссылки» (админ/создатель) "
                    "или откройте у группы публичную ссылку."}

    step = 0
    _given_back_n = 0
    _last_rebalance = time.monotonic()
    _REBALANCE_EVERY_S = 20.0  # не долбить БД COUNT(*) на каждый батч

    # Ограничитель одновременных инвайтов флота. Разные аккаунты бьют по сети
    # независимо; семафор держит не больше _parallel одновременно, чтобы не
    # открыть разом сотню соединений и не залить чат всем флотом в одну секунду.
    _invite_sem = asyncio.Semaphore(_parallel)

    async def _fire(acc, batch, by_phone):
        """Разослать ОДИН батч одним аккаунтом (сеть). Исключение не роняет круг:
        возвращаем его как результат — решение «ретайрить ли аккаунт» на уровне
        обработки (Фаза C), как было в прежнем try/except вокруг батча."""
        async with _invite_sem:
            try:
                if by_phone:
                    # Телефоны идут импортом контакта: промоут-трюк требует уже
                    # резолвнутой сущности, поэтому номера всегда обычным путём.
                    return await inv.invite_by_phones(acc["session_str"], dict(acc), group, batch)
                if _invite_method == "admin":
                    # «Через админку»: цель промоутится в админы (добавляется в
                    # чат) → права тут же снимаются → остаётся участником.
                    return await inv.add_via_promote(acc["session_str"], dict(acc), group, batch)
                if _invite_method == "link":
                    # «Ссылка в ЛС»: рассылаем цель ссылку-приглашение.
                    return await inv.invite_via_link_batch(
                        acc["session_str"], dict(acc), _invite_link, batch,
                        _link_msg, _pace_mult)
                # Множитель темпа уходит ВНУТРЬ батча (см. invite_batch).
                return await inv.invite_batch(
                    acc["session_str"], dict(acc), group, batch, _pace_mult,
                    bulk=_bulk_api)
            except Exception as exc:  # не BaseException: отмену операции пропускаем наверх
                return exc

    async def _iter_round(active):
        """Один круг флота. Набираем батчи чанками по _parallel и рассылаем чанк
        ПАРАЛЛЕЛЬНО, отдавая результаты вызывающему по одному. Набор целей,
        суточные лимиты, governor чата (safe-режим) — ровно как раньше; ново лишь
        то, что сеть нескольких аккаунтов идёт одновременно, а не по очереди.

        Между чанками сверяем стоп-флаги (шторм флудов / закрытая группа /
        отмена / лимит), чтобы флот останавливался так же рано, как в
        последовательном режиме — не «уже разослали всем, потом заметили»."""
        nonlocal group_broken, _group_broken_reason
        _ai = 0
        while _ai < len(active):
            if group_broken or flood_storm:
                return
            if await _is_cancelled(pool, op_id):
                return
            if _max_invites and step >= _max_invites:
                return

            # ── Фаза A: набрать чанк до _parallel батчей (без тяжёлой сети) ──
            _chunk: list = []
            _chunk_planned = 0
            while _ai < len(active) and len(_chunk) < _parallel:
                acc = active[_ai]
                _ai += 1
                if not q_users and not q_phones:
                    break
                acc_id = int(acc["id"])
                if acc_id not in budget:
                    budget[acc_id] = await _acc_budget(acc)
                cap = budget[acc_id]
                if cap <= 0:
                    retired.add(acc_id)
                    await _release_admin_seat(acc_id)
                    # Выбыл по суточному лимиту, ни разу не сработав → «не
                    # задействовали» именно из-за лимита (для честного итога).
                    if acc_id not in _used_accounts:
                        _daily_capped.add(acc_id)
                    continue

                take_n = min(batch_size, cap)
                if _max_invites:
                    take_n = min(take_n, _max_invites - step - _chunk_planned)
                if take_n <= 0:
                    break

                # Сначала опустошаем очередь user_refs, потом телефоны: телефоны
                # дороже (импорт контакта) и оставлять их на конец безопаснее.
                by_phone = not q_users
                queue = q_phones if by_phone else q_users
                batch = _take(queue, take_n)
                if not batch:
                    continue

                # Безопасный режим: governor уровня ЧАТА решает, можно ли слать.
                # abort — чат «мёртвый»/убивает приглашённых, стоп всей операции.
                # Иначе — ждём окно частоты/паузу чата и возвращаем batch (цели
                # не тратим на отказ).
                if _safe_mode:
                    from services import smart_invite as _si
                    _dec = await _si.can_invite(pool, owner_id, _group_key)
                    if not _dec.allowed:
                        _give_back(queue, batch)
                        if _dec.abort:
                            # Ключ governor'а — от МАТЕРИ (см. _group_key): после
                            # ротации у новой дочерней истории нет, проверка увидит
                            # «нет данных» и поведёт себя ОСТОРОЖНЕЕ, а не рискованнее.
                            if await _rotate_daughter(_dec.reason):
                                continue
                            group_broken = True
                            _group_broken_reason = _dec.reason
                            log.warning("mass_invite op=%d: safe-режим СТОП по чату: %s",
                                        op_id, _dec.reason)
                            break  # уже набранный чанк дошлём, дальше круг остановится
                        _wait = max(1.0, min(float(_dec.wait_sec), 120.0))
                        log.info("mass_invite op=%d: safe-режим пауза чата %.0fс: %s",
                                 op_id, _wait, _dec.reason)
                        await asyncio.sleep(_wait)
                        continue

                _chunk.append((acc, acc_id, cap, batch, queue, by_phone))
                _chunk_planned += len(batch)

            if not _chunk:
                # Ничего не набрали (все выбыли/на паузе/лимит) — не крутим пусто.
                if _ai >= len(active) or not (q_users or q_phones):
                    return
                continue

            # ── Фаза B: разослать чанк ПАРАЛЛЕЛЬНО ──
            _results = await asyncio.gather(*[
                _fire(acc, batch, by_phone)
                for (acc, acc_id, cap, batch, queue, by_phone) in _chunk])

            # ── Фаза C у вызывающего: отдаём результаты по одному ──
            for (acc, acc_id, cap, batch, queue, by_phone), res in zip(_chunk, _results):
                yield acc, acc_id, cap, batch, queue, by_phone, res

    while (q_users or q_phones) and not group_broken and not flood_storm:
        if await _is_cancelled(pool, op_id):
            break
        if _max_invites and step >= _max_invites:
            log.info("mass_invite op=%d: достигнут лимит за прогон (%d)", op_id, _max_invites)
            break

        # Честный дележ на лету: другая операция владельца могла стартовать
        # ПОСЛЕ того, как эта захватила весь свободный флот — отдаём лишнее,
        # чтобы обе шли постепенно-параллельно, а не по очереди «до конца».
        if time.monotonic() - _last_rebalance >= _REBALANCE_EVERY_S:
            _last_rebalance = time.monotonic()
            _before_n = len(accounts)
            accounts = await _rebalance_claim(op_id, owner_id, accounts)
            if len(accounts) < _before_n:
                _given_back_n += _before_n - len(accounts)
                retired = {a for a in retired if a in {int(x["id"]) for x in accounts}}
                budget = {k: v for k, v in budget.items() if k in {int(x["id"]) for x in accounts}}

        active = [a for a in accounts if int(a["id"]) not in retired]
        if not active:
            log.info(
                "mass_invite op=%d: свободных аккаунтов не осталось, в очереди %d целей",
                op_id, len(q_users) + len(q_phones),
            )
            break

        progressed = False
        # Круг флота: _iter_round набирает батчи чанками по _parallel и рассылает
        # их ПАРАЛЛЕЛЬНО, отдавая результаты по одному. Вся обработка результата
        # ниже — последовательная (без гонок по общему состоянию).
        async for acc, acc_id, cap, batch, queue, by_phone, res in _iter_round(active):
            progressed = True
            if isinstance(res, Exception):
                # Батч не отработан вовсе — цели возвращаем в очередь (их подберёт
                # другой аккаунт), а этот выводим из круга, чтобы не зациклиться.
                log.warning("mass_invite op=%d acc=%s batch error: %s", op_id, acc.get("id"), res)
                _give_back(queue, batch)
                retired.add(acc_id)
                await _release_admin_seat(acc_id)
                _noconnect_errs.append(str(res)[:160])
                continue

            # Нет прав админа у ЭТОГО аккаунта — проблема аккаунта, НЕ группы/целей.
            # Правильное поведение: аккаунт С правами (промоутер) ВЫДАЁТ права
            # безправному, чтобы работал весь флот, а не только создатель. При
            # первом no_rights пробуем на лету: инвайтер join'ит чат, промоутер
            # выдаёт ему invite_users — и аккаунт остаётся в круге (в следующем
            # круге он уже с правами). Только если промоутера нет ИЛИ выдача уже
            # была и не помогла — выводим из круга (анти-цикл), не роняя прогон.
            if res.get("no_rights"):
                _give_back(queue, batch)  # цели вернуть — их возьмёт кто-то с правами
                _tries = int(_no_rights_on_demand.get(acc_id, 0))
                _reason = "no_promoter"
                # Внешний гейт обязан пропускать до САМОГО щедрого бюджета (30,
                # admins_too_much) — иначе аккаунт, реально ждущий ротации места,
                # обрывался бы после 3-й попытки, так и не дождавшись своей
                # очереди: причина текущей попытки известна только ПОСЛЕ звонка
                # в Telegram, а не до него. За точный бюджет для КАЖДОЙ причины
                # отвечает _retry_ok ниже, уже по факту свежего отказа.
                if _promoter is not None and _irec_promote_retry_allowed(
                        _tries, _MAX_ADMIN_SEAT_WAIT_ATTEMPTS):
                    _no_rights_on_demand[acc_id] = _no_rights_on_demand.get(acc_id, 0) + 1
                    try:
                        from services import account_manager as _am
                        _u = _acc_uid.get(acc_id)
                        if not _u:
                            _u = await asyncio.wait_for(
                                _am.resolve_self_user_id(acc["session_str"], dict(acc)), timeout=30)
                            if _u:
                                _acc_uid[acc_id] = int(_u)
                        if _u:
                            # Инвайтер должен быть участником, затем выдаём права.
                            # Сбой вступления НЕ глушим молча: именно он приводит к
                            # not_participant на следующем шаге, и знать об этом надо.
                            try:
                                await asyncio.wait_for(
                                    _am.join_channel(acc["session_str"], group, _acc=dict(acc)),
                                    timeout=45)
                            except Exception as _je:
                                log.info("mass_invite op=%d acc=%s: вступление не удалось "
                                         "(%s) — права всё равно пробуем выдать",
                                         op_id, acc.get("id"), str(_je)[:80])
                            # Telegram регистрирует членство не мгновенно: со второй
                            # попытки даём ему секунду-другую, иначе снова получим
                            # not_participant и потеряем рабочий аккаунт.
                            if _tries:
                                await asyncio.sleep(min(5, 2 * _tries))
                            _okp, _reason = await asyncio.wait_for(
                                _am.promote_to_admin_ex(
                                    _promoter["session_str"], group, int(_u),
                                    _acc=dict(_promoter), invite_users=True, post_messages=False,
                                    add_admins=(_invite_method == "admin")),
                                timeout=45)
                            if _okp:
                                _promoted_n += 1
                                _admin_seats.add(acc_id)
                                log.info("mass_invite op=%d acc=%s: права выданы на лету — "
                                         "остаётся в круге", op_id, acc.get("id"))
                                continue  # НЕ ретайрим — попробует снова уже с правами
                        else:
                            _reason = "no_uid"
                    except Exception as _e:
                        _reason = "error"
                        log.debug("mass_invite op=%d acc=%s: on-demand promote failed: %s",
                                  op_id, acc.get("id"), _e)

                    # Временный отказ — аккаунт остаётся в круге и попробует в
                    # следующем круге, пока не исчерпает попытки. Окончательный
                    # (у промоутера нет права add_admins) повторять бессмысленно:
                    # тратить на него попытки — терять аккаунт медленнее, но так же.
                    #
                    # admins_too_much — отдельный, более терпеливый бюджет попыток:
                    # это не сетевая заминка, а ожидание своей очереди на РОТАЦИЮ
                    # места (см. _release_admin_seat) — она освобождается не по
                    # таймеру, а когда ДРУГОЙ аккаунт отработает свой батч и
                    # выбудет из круга, а это может занять много раундов.
                    _tries_now = _no_rights_on_demand[acc_id]
                    _retry_ok = (
                        _irec_promote_retry_allowed(_tries_now)
                        if _reason in ("not_participant", "flood", "error")
                        else _irec_promote_retry_allowed(_tries_now, _MAX_ADMIN_SEAT_WAIT_ATTEMPTS)
                        if _reason == "admins_too_much"
                        else False
                    )
                    if _retry_ok:
                        log.info("mass_invite op=%d acc=%s: выдача прав не прошла (%s), "
                                 "попытка %d — аккаунт остаётся в круге",
                                 op_id, acc.get("id"), _reason, _tries_now)
                        continue

                # Промоутера нет / попытки исчерпаны / отказ окончательный
                retired.add(acc_id)
                await _release_admin_seat(acc_id)
                _no_rights_retired += 1
                log.info("mass_invite op=%d acc=%s: нет прав админа, выдать не удалось (%s) — "
                         "выведен из круга, продолжаем аккаунтами с правами",
                         op_id, acc.get("id"), _reason)
                continue

            ok_n = int(res.get("ok") or 0)
            fail_n = int(res.get("failed") or 0)
            # Движок обрывает батч на флуде/закрытой группе — хвост НЕ пробовали.
            attempted = max(0, min(len(batch), ok_n + fail_n))
            leftover = batch[attempted:]
            if attempted:
                if by_phone:
                    # Дедупим только УСПЕШНО приглашённые номера; «не в Telegram»
                    # не помечаем — их можно пробовать позже.
                    invited_this_run.update(res.get("invited_phones") or [])
                    _phones_not_found += len(res.get("not_found_phones") or [])
                else:
                    invited_this_run.update(batch[:attempted])
            total_ok += ok_n
            total_fail += fail_n

            # Безопасный режим: копим состояние по чату и держим темп.
            if _safe_mode:
                from services import smart_invite as _si
                # Пакетно: раньше здесь было по вызову на цель, и учёт съедал до
                # двадцати обращений к БД на батч из пяти — последовательно,
                # внутри цикла инвайта, то есть прямо замедляя прогон.
                await _si.note_sent(pool, owner_id, _group_key, n=attempted)
                await _si.note_outcome(pool, owner_id, _group_key, "joined", n=ok_n)
                # Флуд при инвайте в чат трактуем и как сигнал заморозки ПРИЁМА —
                # чат отвечает flood не только аккаунту. Ставим паузу чата.
                if res.get("peer_flood") or res.get("flood_wait"):
                    await _si.note_outcome(pool, owner_id, _group_key, "chat_flood")
                # Пауза между батчами по решению governor'а (джиттер, без ровной
                # машинной частоты) — только если что-то реально отправили.
                if attempted:
                    _d2 = await _si.can_invite(pool, owner_id, _group_key)
                    if _d2.next_delay_sec > 0:
                        await asyncio.sleep(min(_d2.next_delay_sec, 120.0))

            step += attempted
            if attempted:
                _used_accounts.add(acc_id)
                _daily_capped.discard(acc_id)  # сработал → уже не «только из-за лимита»
            budget[acc_id] = max(0, cap - attempted)
            if attempted:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+$2 WHERE id=$1",
                    op_id, attempted,
                )
            if leftover:
                _give_back(queue, leftover)

            # Цели, отклонённые приватностью — кандидаты на промоут-трюк.
            if _promoter is not None and _promote_trick:
                _privacy_blocked.extend(res.get("privacy_failed") or [])
            # Классифицируем причины отказов + ПИШЕМ по каждой цели в лог операции,
            # чтобы CSV показывал, КТО и ПОЧЕМУ не добавлен (раньше per-target лога
            # инвайта не было — «Ошибок: 7» без деталей).
            # Причины отказов движок теперь отдаёт СТРУКТУРОЙ (fail_kinds).
            # Разбор английского текста ошибок оставлен запасным путём — для
            # движков «через админку» и «ссылка в ЛС», которые его пока не дают.
            _kinds = res.get("fail_kinds")
            if _kinds:
                for _k, _n in _kinds.items():
                    _fail_reasons[_k] = _fail_reasons.get(_k, 0) + int(_n)
            for _e in (res.get("errors") or []):
                if not _kinds:
                    _es = str(_e).lower()
                    if "privacy" in _es:
                        _k = "privacy"
                    elif "not mutual" in _es:
                        _k = "not_mutual"
                    elif _es.startswith("group error"):
                        _k = "perm"
                    elif "flood" in _es:
                        _k = "flood"
                    else:
                        _k = "other"
                    _fail_reasons[_k] = _fail_reasons.get(_k, 0) + 1
                # "{ref}: reason" → target=ref, message=reason; иначе цель — группа.
                _raw = str(_e)
                if _raw.startswith("group error"):
                    _tgt, _msg = "группа", _raw.split(":", 1)[-1].strip()
                elif ": " in _raw:
                    _tgt, _msg = _raw.split(": ", 1)
                else:
                    _tgt, _msg = "—", _raw
                await _safe_execute(
                    pool, "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'fail',$4)", op_id, step, _tgt[:120], _msg[:200])

            # Успешно добавленные цели — тоже в лог (для полного CSV-отчёта: КОГО
            # добавили, а не только кого нет). Успех = попробованные минус явные
            # провалы (приватность + разобранные из errors). Батчем, с потолком.
            if _ok_logged < _OK_LOG_CAP and attempted:
                if by_phone:
                    _succ = [str(x) for x in (res.get("invited_phones") or [])]
                else:
                    _failed_set = {str(x) for x in (res.get("privacy_failed") or [])}
                    for _e in (res.get("errors") or []):
                        _raw = str(_e)
                        if ": " in _raw and not _raw.startswith("group error"):
                            _failed_set.add(_raw.split(": ", 1)[0])
                    _succ = [str(r) for r in batch[:attempted] if str(r) not in _failed_set]
                # без дублей в отчёте (одну цель могли пробовать в двух батчах)
                _succ = [s for s in _succ if s not in _ok_seen]
                _ok_seen.update(_succ)
                _succ = _succ[:max(0, _OK_LOG_CAP - _ok_logged)]
                if _succ:
                    try:
                        # МАРКЕР ИСХОДА. Ретеншен инвайта (экран мини-аппа и
                        # подсказка организма) считает вступивших запросом
                        # `status='ok' AND message='joined'` — а маркер не писал
                        # НИКТО. Обе функции показывали «нет данных» с самого
                        # своего появления: приток всегда выходил нулём.
                        #
                        # Маркер не декоративный: голого 'ok' для этого мало.
                        # У метода «ссылка в ЛС» успех означает доставленную
                        # ссылку, а не вступление (человек ещё может не войти),
                        # и складывать их в один счётчик значило бы завышать
                        # ретеншен. Поэтому исход называется явно.
                        _outcome = "link_sent" if _invite_method == "link" else "joined"
                        await pool.executemany(
                            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                            "VALUES($1,$2,$3,'ok',$4)",
                            [(op_id, step, s[:120], _outcome) for s in _succ])
                        _ok_logged += len(_succ)
                    except Exception:
                        log_exc_swallow(log, "invite: ok per-target log")

            # Группа закрыта/нет прав — это не про аккаунт, это про цель. Раньше
            # об неё по очереди разбивался весь флот; теперь останавливаемся сразу.
            _gerr = next((str(e) for e in (res.get("errors") or [])
                          if str(e).startswith("group error")), "")
            if _gerr:
                _group_reason = _gerr.split("group error:", 1)[-1].strip()
                await bump_daily_stats(pool, acc_id, ok=ok_n, fail=fail_n, invites=ok_n)
                if await _rotate_daughter(_group_reason):
                    continue
                group_broken = True
                log.warning(
                    "mass_invite op=%d: группа %s недоступна (%s) — остановка, чтобы не жечь флот",
                    op_id, group, _group_reason[:120],
                )
                # НЕ break: батчи текущего чанка уже разосланы параллельно —
                # их результаты нужно досчитать. Флаг group_broken не даст
                # _iter_round набрать следующий чанк, и круг остановится.
                continue

            if res.get("peer_flood") or res.get("flood_wait"):
                log.warning(
                    "mass_invite op=%d acc=%s %s — cooldown+switch", op_id, acc.get("id"),
                    "PeerFlood" if res.get("peer_flood") else f"FloodWait {res.get('flood_wait')}s",
                )
                await bump_daily_stats(pool, acc_id, floods=1, ok=ok_n,
                                       fail=fail_n, invites=ok_n)
                await _rest_invite_account(acc["id"], res)
                retired.add(acc_id)
                await _release_admin_seat(acc_id)
                # Стоп-кран: подряд-флуды без успеха = флот перегрет. Успех сбросил
                # бы счётчик ниже; здесь — только флуды. При пороге останавливаем всю
                # операцию, чтобы не жечь оставшиеся аккаунты в бан.
                flood_streak += 1
                _now_f = time.monotonic()
                _flood_times.append(_now_f)
                # в окне держим только свежие отметки
                _flood_times[:] = [t for t in _flood_times if _now_f - t <= _flood_window_sec]
                # Суточный (длинный) флуд считаем отдельно и вне окна: это уже бан на
                # приглашения, а не темп. Успех его НЕ обнуляет — потолок никуда не делся.
                if int(res.get("flood_wait") or 0) >= _long_flood_s:
                    _long_floods += 1
                if _flood_stop_streak or _long_flood_stop:
                    # Шторм — это кучность флудов, а не их общее число за прогон.
                    _storm_now = _flood_stop_streak and len(_flood_times) >= _flood_stop_streak
                    # Отдельный случай: флот не дал НИ ОДНОГО успеха и уже столько
                    # раз получил флуд. Тогда дело не в хвосте очереди — работать
                    # нечем, и продолжать значит жечь аккаунты впустую.
                    _dead_start = (total_ok == 0 and _flood_stop_streak
                                   and flood_streak >= _flood_stop_streak)
                    # Несколько суточных банов = флот у жёсткого потолка по этому чату:
                    # каждый следующий аккаунт тоже уйдёт в суточный бан. Стоп раньше.
                    _long_storm = _long_flood_stop and _long_floods >= _long_flood_stop
                    if _storm_now or _dead_start or _long_storm:
                        flood_storm = True
                        log.warning(
                            "mass_invite op=%d: стоп операции (флот перегрет) — "
                            "%d флудов за %dс, %d суточных банов%s",
                            op_id, len(_flood_times), _flood_window_sec, _long_floods,
                            (", успехов нет вовсе" if _dead_start else
                             ", флот у суточного потолка приглашений" if _long_storm else ""),
                        )
                        # НЕ break: результаты уже разосланного чанка досчитываем;
                        # флаг flood_storm остановит набор следующего чанка.
                continue

            if attempted == 0:
                # Ни одной попытки без флуда = аккаунт не смог подключиться.
                _errs = "; ".join(str(e) for e in (res.get("errors") or []))
                log.warning(
                    "mass_invite op=%d acc=%s: батч не отработан (%s) — выведен из круга",
                    op_id, acc.get("id"), _errs[:120],
                )
                retired.add(acc_id)
                await _release_admin_seat(acc_id)
                _noconnect_errs.append(_errs[:160] or "аккаунт не ответил (сессия/сеть)")
                continue

            await bump_daily_stats(pool, acc_id, ok=ok_n, fail=fail_n, invites=ok_n)
            if ok_n:
                flood_streak = 0  # реальный успех разрывает серию флудов
            await _invite_learn_ok(acc_id, ok_n)
            if budget[acc_id] <= 0:
                # Аккаунт честно отработал свой батч до конца — ГЛАВНЫЙ случай
                # ротации мест админа: если он держал место (был промоутнут),
                # оно освобождается для следующего в очереди ПРЯМО СЕЙЧАС, а не
                # простаивает у отработавшего аккаунта до самого конца прогона.
                retired.add(acc_id)
                await _release_admin_seat(acc_id)
            await _humanize(acc)
            await asyncio.sleep(_invite_pause(acc_id))

        if not progressed:
            break

    # ── Промоут-трюк для заблокированных приватностью целей ───────────────────
    # Прямой инвайт их не взял; аккаунт-админ с add_admins пробует добавить через
    # выдачу/снятие админки. Ограничиваем объём, уважаем флуд/отмену/лимит прогона.
    _trick_ok = 0
    if _promoter is not None and _promote_trick and _privacy_blocked and not group_broken \
            and not flood_storm and not await _is_cancelled(pool, op_id):
        from services import mass_inviter_engine as _inv
        _seen_pt: set = set()
        _uniq_blocked = [x for x in _privacy_blocked
                         if not (str(x) in _seen_pt or _seen_pt.add(str(x)))]
        _cap = 200 if not _max_invites else max(0, _max_invites - (total_ok + total_fail))
        _uniq_blocked = _uniq_blocked[:max(0, _cap)] if _cap else _uniq_blocked[:200]
        if _uniq_blocked:
            log.info("mass_invite op=%d: промоут-трюк для %d заблокированных целей",
                     op_id, len(_uniq_blocked))
            try:
                _tr = await _inv.add_via_promote(
                    _promoter["session_str"], dict(_promoter), group, _uniq_blocked)
                _trick_ok = int(_tr.get("ok") or 0)
                total_ok += _trick_ok
                if _trick_ok:
                    await bump_daily_stats(pool, int(_promoter["id"]),
                                           ok=_trick_ok, invites=_trick_ok)
                    invited_this_run.update(str(x) for x in _uniq_blocked)
                    await _safe_execute(
                        pool, "UPDATE operation_queue SET done_items=done_items+$2 WHERE id=$1",
                        op_id, _trick_ok)
                await _safe_execute(
                    pool, "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,0,'promote_trick',$2,$3)", op_id,
                    "ok" if _trick_ok else "fail",
                    f"добавлено промоут-трюком: {_trick_ok}/{len(_uniq_blocked)}")
            except Exception as _te:
                log.warning("mass_invite op=%d: промоут-трюк сбой: %s", op_id, _te)

    # Запомнить обработанные цели, чтобы следующий прогон их не тыкал повторно.
    await _record_invited_targets(pool, owner_id, _group_key, op_id, invited_this_run)

    total = total_ok + total_fail
    _left = len(q_users) + len(q_phones)

    # Человекочитаемый разбор ошибок — чтобы «Ошибок: 7» не читалось как поломка.
    _reason_labels = dict(inv.FAIL_LABELS)
    _fail_breakdown = " · ".join(
        f"{_reason_labels.get(k, k)}: {v}"
        for k, v in sorted(_fail_reasons.items(), key=lambda kv: -kv[1]) if v)
    # Подсказка по КРУПНЕЙШЕЙ корзине отказов: цифра без действия оператору
    # ничего не даёт — «👻 нет в Telegram: 812» означает «почистите базу», а
    # «🔒 приватность: 812» — «смените метод на ссылку в ЛС».
    _top_reason = max(_fail_reasons.items(), key=lambda kv: kv[1])[0] if _fail_reasons else ""
    _fail_advice = inv.FAIL_ADVICE.get(_top_reason, "") if total_fail else ""

    # ── Остаток не бросаем: продолжим, когда лимиты обновятся ────────────────
    # Суточный лимит на аккаунт консервативен по умолчанию (холодный старт — 15).
    # У пользователя с одним-двумя аккаунтами и большой аудиторией первый прогон
    # закрывает лишь её часть. Раньше остаток исчезал вместе с операцией: в итоге
    # писалось «осталось N», и дальше пользователь должен был вспомнить и
    # запустить заново. Снаружи это выглядит как «инвайт не доработал» или вовсе
    # «не работает».
    #
    # Теперь остаток уезжает в ОТДЕЛЬНУЮ операцию на завтра. Не продолжаем, если
    # флот перегрет (flood_storm) или группа закрыта — там проблема не в лимитах,
    # и повтор только навредит.
    # Все аккаунты не подключились (сессия/сеть) и ни один не сработал — это НЕ про
    # суточные лимиты. Продолжать «завтра» бессмысленно: те же мёртвые/задублированные
    # сессии упадут так же (и так до _MAX_INVITE_CHAIN дней холостых операций). Не
    # планируем продолжение — вместо ложной надежды даём честную причину и что делать.
    _all_failed_connect = (len(_used_accounts) == 0) and bool(_noconnect_errs)

    _next_op = None
    _chain = int(params.get("invite_chain") or 0)
    # Перегрев флота больше НЕ отменяет продолжение: раньше остаток целей
    # исчезал вместе с операцией, хотя отчёт сам советовал «дайте отдохнуть и
    # повторите позже». PeerFlood/FloodWait лечатся отдыхом, и продолжение
    # уезжает на следующий запуск — так цель достигается без сжигания флота.
    from services import invite_recovery as _irec

    _cont_ok, _cont_why = _irec.should_schedule_continuation(
        left=_left, group_broken=group_broken,
        all_failed_connect=_all_failed_connect,
        chain=_chain, max_chain=_MAX_INVITE_CHAIN, flood_storm=flood_storm,
        ok_count=total_ok)
    if _cont_ok:
        if not await _is_cancelled(pool, op_id):
            _next_op = await _schedule_invite_continuation(
                pool, owner_id, params, list(q_users), list(q_phones), _chain + 1)

    # Честный учёт задействования аккаунтов (ответ на «выбрал 28, работали ~20»).
    # Не задействованы = отобранные, прошедшие карантин, но ни разу не сработавшие
    # (суточный лимит / мёртвая сессия / сбой подключения).
    _engaged_n = len(_used_accounts)
    _idle_n = max(0, len(accounts) - _engaged_n)
    _acc_parts = []
    if _quarantined_n:
        _acc_parts.append(f"🚫 в карантине (риск бана): {_quarantined_n}")
    if _include_risky and _risky_forced:
        _acc_parts.append(f"⚠️ включены рисковые ПРИНУДИТЕЛЬНО (кулдаун/карантин сняты): {_risky_forced}")
    if _daily_capped:
        _acc_parts.append(f"⏳ исчерпан суточный лимит: {len(_daily_capped)}")
    if _given_back_n:
        _acc_parts.append(f"🔀 отдано другим параллельным операциям владельца: {_given_back_n}")
    _idle_other = _idle_n - len(_daily_capped)
    if _idle_other > 0:
        _acc_parts.append(f"⚪ не ответили (сессия/сеть): {_idle_other}")
    # Предупреждение о риске: аккаунты БЕЗ назначенного прокси могли работать
    # напрямую с IP хоста (разрешено политикой allow_direct как последний резерв,
    # но это риск блокировок). Показываем оператору — это его сигнал назначить прокси.
    _no_proxy_n = sum(
        1 for a in accounts
        if int(a["id"]) in _used_accounts
        and not (str(a.get("proxy_url") or "").strip() or a.get("proxy_id")))
    _proxy_warn = (
        f"\n⚠️ Без прокси (прямой выход с IP хоста, риск блокировок): {_no_proxy_n} — "
        "назначьте прокси для изоляции." if _no_proxy_n else "")
    _acc_line = (
        f"\n👤 Аккаунты: работали {_engaged_n} из {_selected_n}"
        + (("\n   " + " · ".join(_acc_parts)) if _acc_parts else "")
        + _proxy_warn
    ) if _selected_n else ""

    # Подсказка по типовой причине сбоя подключения (для «одного прохода без ошибок»):
    # сырой английский текст Telethon пользователю не нужен — нужна причина + действие.
    _sess_hint = ""
    if _noconnect_errs:
        _blob = " ".join(_noconnect_errs).upper()
        if "TWO DIFFERENT IP" in _blob or "AUTH_KEY_DUPLICATED" in _blob:
            _sess_hint = (
                "\n⚠️ Кратковременный конфликт: сессия шла с двух IP одновременно "
                "(AUTH_KEY_DUPLICATED). Аккаунты НЕ отключены — это временно. Обычно "
                "это ЭТА ЖЕ сессия, активная где-то ещё (телефон/другой софт), либо "
                "нестабильный IP без прокси. Дайте каждому аккаунту свой прокси "
                "(стабильный IP); если нужен отдельный от телефона сеанс — переавторизуйте "
                "здесь: у каждого устройства свой ключ, они спокойно сосуществуют. "
                "Повторите инвайт — при снятии конфликта аккаунты подключатся."
            )
        elif ("AUTH_KEY_UNREGISTERED" in _blob or "SESSION_REVOKED" in _blob
              or "SESSION_EXPIRED" in _blob or "AUTHORIZATION KEY" in _blob):
            _sess_hint = ("\n⚠️ Сессии недействительны — переавторизуйте аккаунты "
                          "в разделе «Аккаунты».")

    # Метод «ссылка в ЛС» не добавляет насильно — честно называем ok доставкой
    # ссылки, а не вступлением (вступление зависит от согласия человека).
    _method_hdr = {
        "admin": "👥 Инвайтер (через админку): ",
        "link": "🔗 Инвайтер (ссылка в ЛС): ",
    }.get(_invite_method, "👥 Инвайтер: ")
    _ok_word = "📨 Ссылка отправлена" if _invite_method == "link" else "✅ Добавлено"
    summary = (
        f"{_method_hdr}{group}\n"
        f"{_ok_word}: {total_ok}/{total}"
        + _acc_line
        # «Мать-Дочка» была не видна в отчёте ВООБЩЕ: ни строки о том, что инвайт
        # шёл в расходную группу, ни счётчика ротаций. Ссылка выше при этом —
        # дочерней группы, а подписана как обычная цель, то есть отчёт выглядел
        # так, будто людей заводили прямо в боевой канал. По такому итогу нельзя
        # понять, работал механизм или нет, — владелец и не мог этого проверить.
        + (f"\n🏪 Витрина: приглашённые попадают в буфер, а в боевой канал их "
           f"пускают волнами по {showcase_layer.wave_size(0)}+ мест — "
           "всплеска вступлений канал не получает."
           if _use_showcase else "")
        + (f"\n🛡 Мать-Дочка: инвайт шёл в расходную группу (ссылка выше), "
           f"боевой канал — {_mother_ref}."
           + (f" Сожжено дочерних за прогон: {_daughter_rotations}."
              if _daughter_rotations else " Дочерняя выдержала прогон.")
           + ("" if _use_showcase else
              "\n   ⚠️ От бана за инвайты это защищает, но приглашённые переходят "
              "в боевой канал по закреплённой ссылке — всплеск вступлений он "
              "получает всё равно. Снимает это витрина (переключатель рядом).")
           if _use_daughter else "")
        + (f"\n🛡 Выдана админка инвайтерам: {_promoted_n}" if _promoted_n else "")
        + (f"\n➕ Добавлено промоут-трюком (обход приватности): {_trick_ok}" if _trick_ok else "")
        + (f"\n♻️ Пропущено уже приглашённых: {_deduped}" if _deduped else "")
        + (f"\n📦 Источник больше {_INVITE_AUDIENCE_CAP} целей — взята первая порция; "
           "остальные подтянутся следующими прогонами." if not _source_exhausted else "")
        + (f"\n🚫 Пропущено из реестра «не приглашать»: {_opted_out}" if _opted_out else "")
        + (f"\n📵 Номеров не в Telegram (пропущены): {_phones_not_found}" if _phones_not_found else "")
        + (f"\n⚠️ Ошибок: {total_fail}"
           + (f"\n   ({_fail_breakdown})" if _fail_breakdown else "")
           + (f"\n   💡 {_fail_advice}" if _fail_advice else "")
           if total_fail else "")
        + (f"\n🤖 Авто-темп: {_auto_reason}" if _auto_reason else "")
        + ("\n🚀 Режим «один проход»: работали до потолка ёмкости по живым сигналам "
           "флуда (без предсказанного суточного лимита)." if _one_pass else "")
        + (f"\n🚫 Не удалось добавлять в чат — операция остановлена.\n   Причина: {_group_reason}"
           if group_broken and _group_reason else
           ("\n🚫 Группа недоступна — операция остановлена" if group_broken else ""))
        # Честное объяснение «почему 0 добавлено», когда дело не в группе/флуде:
        # приватность собеседников добавить нельзя (это ограничение Telegram, не бага).
        + (("\n🔒 Большинство отклонены приватностью получателей — таких Telegram "
            "запрещает добавлять в группы; помогает только их согласие/старый общий чат."
           ) if (total_ok == 0 and not group_broken and not flood_storm
                 and _fail_reasons.get("privacy", 0) + _fail_reasons.get("not_mutual", 0)
                     >= max(1, int(total_fail * 0.5))) else "")
        # Про повтор больше НЕ советуем словами: остаток планируется сам (строка
        # «🔁 Осталось …» ниже). Раньше здесь висел совет «повторите позже», а
        # продолжение при перегреве не создавалось вовсе — цель просто терялась.
        #
        # Счётчик — из окна времени, а не «подряд»: прежняя формулировка
        # обещала пользователю серию, которой не было (6 флудов за 2 часа).
        + (f"\n🛑 Флот перегрет: {len(_flood_times)} флудов за "
           f"{_flood_window_sec // 60} мин — операция остановлена, чтобы не потерять "
           "аккаунты. Кучные флуды означают, что Telegram отвечает флоту как группе; "
           "это временный лимит и снимается отдыхом."
           if flood_storm else "")
        + (f"\n🚫 {_no_rights_retired} аккаунтов без прав админа выведены из круга — их "
           "цели переданы аккаунтам с правами (если инвайтит только создатель, проверьте, "
           "что автовыдача админки прошла)." if _no_rights_retired else "")
        # Честная причина «вступили, но не инвайтят»: без прав приглашать инвайт в
        # чат/канал не стартует. Раньше это пряталось в лог шага promote — теперь в итоге.
        + ("\n🛡 Некому выдать право приглашать: ни один ваш аккаунт не админ этого чата "
           "с правом «Назначать администраторов». Сделайте один аккаунт админом чата (с этим "
           "правом) и включите его в операцию — он автоматически выдаст право приглашать "
           "остальным. Без этого аккаунты вступают, но не инвайтят."
           if _promote_skipped_no_admin and total_ok == 0 else "")
        + (f"\n🛡 Промоутер есть, но {_promote_no_uid} инвайтерам не выдалась админка — "
           "не удалось получить их user_id. Переавторизуйте эти аккаунты в разделе «Аккаунты»."
           if _promote_no_uid and _promoted_n == 0 and total_ok == 0
           and not _promote_skipped_no_admin else "")
        + _sess_hint
        + (f"\n🛑 Инвайт не выполнен: ни один аккаунт не подключился. Остаток {_left} "
           "НЕ отложен — сначала почините аккаунты (см. выше), иначе повтор бесполезен."
           if _all_failed_connect else "")
        + (f"\n🔁 Осталось {_left} — продолжим завтра автоматически (операция #{_next_op})"
           if _next_op else "")
        + (f"\n⏸ Осталось в очереди: {_left} (аккаунты исчерпали лимит на сегодня)"
           if _left and not _next_op and not group_broken and not flood_storm
           and not _all_failed_connect else "")
    )
    # Шов «Инвайт → Welcome»: приветствие реально добавленным (если настроено).
    await _chain_welcome(pool, owner_id, op_id, params)
    # no_proxy / no_rights_retired отдаём СТРУКТУРНО, а не только текстом сводки:
    # по ним строятся быстрые действия («назначить прокси», «проверить права»).
    # Раньше отчёт советовал это словами, а кнопки не было — совет уходил в пустоту.
    return {"status": "done", "ok": total_ok, "failed": total_fail,
            "left": _left, "next_op_id": _next_op,
            "worked_accounts": _engaged_n, "noconnect": _idle_other,
            "all_failed_connect": _all_failed_connect,
            "no_proxy": _no_proxy_n, "no_rights_retired": _no_rights_retired,
            "flood_storm": flood_storm, "summary": summary}


# ── Сеттер профилей ───────────────────────────────────────────────────────────

async def _exec_bulk_set_profile(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое оформление аккаунтов: имя/bio, аватар, 2FA."""
    from services import profile_setter_engine as pse

    op = params.get("op", "")
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Anti-detection (#7): отсеять аккаунты в карантине ПЕРЕД действием — действие
    # с флагнутого (флуд/ограничение) аккаунта = быстрый бан. Общий гейт, fail-open.
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count, fail_count = 0, 0
    total = len(accounts)

    for idx, acc in enumerate(accounts, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            # Единый диспетчер op→движок (та же реализация, что инлайн-путь). Спинтакс
            # раскрывается внутри apply_op — для каждого аккаунта отдельно.
            res = await pse.apply_op(acc["session_str"], dict(acc), op, params)

            if res["ok"]:
                ok_count += 1
                # «Проверка» — фиксируем сам вердикт в лог, чтобы был виден результат
                if op == "check_restriction":
                    verdict = pse.format_restriction_verdict(res)
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                        "VALUES($1,$2,$3,'ok',$4)",
                        op_id, idx, f"acc#{acc['id']}", verdict[:200],
                    )
                else:
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                        op_id, idx, f"acc#{acc['id']}",
                    )
            else:
                fail_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                )
        except Exception as exc:
            log.warning("bulk_set_profile op=%d acc=%s: %s", op_id, acc.get("id"), exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 2.0))

    op_labels = {"name": "Имя/Bio", "avatar": "Аватар", "2fa": "2FA пароль",
                 "username": "Username", "bio": "Bio", "close_sessions": "Закрыть сессии",
                 "privacy": "Приватность",
                 "clear_bio": "Очистить bio", "remove_username": "Снять username",
                 "remove_avatar": "Удалить фото", "reset_2fa": "Снять 2FA",
                 "set_online": "В сети", "check_restriction": "Проверка ограничений"}
    summary = (
        f"🎨 Сеттер: {op_labels.get(op, op)}\n"
        f"✅ Успешно: {ok_count}/{total}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
    )
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


# ── Репортер ──────────────────────────────────────────────────────────────────

async def _exec_mass_report(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовые жалобы на профиль/канал или сообщения."""
    from services import reporter_engine as rep

    mode = params.get("mode", "peer")
    target = params.get("target", "")
    reason = params.get("reason", "spam")
    report_text = params.get("report_text", "")
    msg_ids = [int(i) for i in (params.get("msg_ids") or [])]
    account_ids = [int(i) for i in (params.get("account_ids") or [])]

    if not target or not account_ids:
        return {"status": "failed", "summary": "⚠️ Неполные параметры mass_report"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Риск-пульс: жалоба с аккаунта под недавним серьёзным ограничением = быстрый бан
    # именно этого аккаунта. Отсеиваем (fail-open: все в карантине → работаем всеми).
    accounts, _skipped_quar = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count, fail_count = 0, 0
    total = len(accounts)

    for idx, acc in enumerate(accounts, 1):
        if await _is_cancelled(pool, op_id):
            break
        try:
            if mode == "msg" and msg_ids:
                res = await rep.report_message(
                    acc["session_str"], dict(acc), target, msg_ids, reason, report_text
                )
            else:
                res = await rep.report_peer(
                    acc["session_str"], dict(acc), target, reason, report_text
                )
            if res["ok"]:
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                    op_id, idx, f"acc#{acc['id']}",
                )
            else:
                fail_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                    op_id, idx, f"acc#{acc['id']}", (res.get("error") or "")[:200],
                )
        except Exception as exc:
            log.warning("mass_report op=%d acc=%s: %s", op_id, acc.get("id"), exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 2.5))

    from services.reporter_engine import REPORT_REASONS
    reason_label = REPORT_REASONS.get(reason, ("?", ""))[0]
    summary = (
        f"🚨 Жалобы на {target} [{reason_label}]\n"
        f"✅ Отправлено: {ok_count}/{total}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
        + (f"\n🛡 Пропущено (риск-пульс): {_skipped_quar}" if _skipped_quar else "")
    )
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


# ── Content Clone executor ──────────────────────────────────────────────────


async def _exec_content_clone(
    pool: asyncpg.Pool,
    bot,
    op_id: int,
    owner_id: int,
    params: dict,
) -> dict:
    """Клонирует сообщения из канала-источника в список каналов-целей."""
    from services.content_cloner_engine import clone_to_channel, get_last_msg_ids

    source_ref: str = params.get("source_ref", "")
    target_refs: list[str] = params.get("target_refs", [])
    mode: str = params.get("mode", "forward")       # forward | copy
    msg_ids: list[int] = params.get("msg_ids", [])
    msg_count: int = int(params.get("msg_count", 10))
    account_ids: list[int] = params.get("account_ids", [])

    if not source_ref or not target_refs:
        return {"status": "failed", "summary": "⚠️ Не указан источник или цели"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, include_ids=[int(_i) for _i in account_ids], min_trust_score=0.0)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Anti-detection (#7): отсеять аккаунты в карантине ПЕРЕД клонированием —
    # действие с флагнутого (флуд/ограничение) аккаунта = быстрый бан. Общий
    # гейт, fail-open (все в карантине → не обнуляем op, берём исходный список).
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    acc = dict(accounts[0])

    # Если msg_ids не переданы — получаем последние msg_count сообщений
    if not msg_ids:
        msg_ids = await get_last_msg_ids(acc["session_str"], acc, source_ref, msg_count)
    if not msg_ids:
        return {"status": "failed", "summary": "⚠️ Не удалось получить сообщения источника"}

    total = len(target_refs)
    await _safe_execute(
            pool,
        "UPDATE operation_queue SET total_items=$1, done_items=0 WHERE id=$2",
        total, op_id,
    )

    ok_count = 0
    fail_count = 0
    cloned_to: list[str] = []

    for idx, target_ref in enumerate(target_refs, 1):
        if await _is_cancelled(pool, op_id):
            return {"status": "cancelled", "summary": f"Отменено на {idx - 1}/{total}"}
        try:
            res = await clone_to_channel(
                acc["session_str"], acc, source_ref, target_ref, msg_ids, mode,
            )
            if res["ok"] > 0:
                ok_count += 1
                cloned_to.append(target_ref)
            else:
                fail_count += 1
                log.warning(
                    "content_clone op=%d target=%s: ok=0 errors=%s",
                    op_id, target_ref, res["errors"][:2],
                )
            # Аккаунт упёрся во флуд — идти следующей целью ТЕМ ЖЕ аккаунтом
            # значит гарантированно получить флуд снова и приблизить бан.
            # Ставим cooldown и либо откладываем операцию, либо останавливаемся.
            _fw = int(res.get("flood_wait") or 0)
            if _fw or res.get("peer_flood"):
                try:
                    from services.flood_engine import record_flood

                    await record_flood(
                        pool, acc["id"], _fw or 3600, "content_clone", op_id)
                except Exception:
                    log_exc_swallow(log, "content_clone: record_flood failed")
                if res.get("peer_flood"):
                    return {
                        "status": "failed",
                        "ok": ok_count,
                        "failed": fail_count + (total - idx),
                        "summary": (
                            f"🚫 Аккаунт ограничен Telegram (peer flood) на {idx}/{total}.\n"
                            f"✅ Успели: {ok_count}"
                        ),
                    }
                if _fw > _FLOOD_INLINE_MAX_S:
                    # Тот же контракт, что у остальных исполнителей: откладывает
                    # операцию сам _run_op_task по defer_s, освобождая слот и флот.
                    return {
                        "status": "requeue",
                        "defer_s": _fw,
                        "reason": f"content_clone: FloodWait {_fw}s на {idx}/{total}",
                    }
                await bounded_flood_sleep(_fw, "content_clone")
        except Exception as exc:
            log.warning("content_clone op=%d target=%s: %s", op_id, target_ref, exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            # межцелевой темп под губернатором (давление флота тормозит)
            await asyncio.sleep(await _governed_delay(pool, owner_id, 1.5))

    mode_label = "Пересылка" if mode == "forward" else "Копирование"
    summary = (
        f"📋 {mode_label} из {source_ref}\n"
        f"📨 Сообщений: {len(msg_ids)} → {total} канал(ов)\n"
        f"✅ Успешно: {ok_count}"
        + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else "")
    )
    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "cloned_to": cloned_to[:50],
        "summary": summary,
    }


async def _exec_ai_comment(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """AI Commenting: под каждым целевым каналом (его группой обсуждений) постит
    контекстный AI-комментарий к недавнему посту. Round-robin по аккаунтам,
    задержки, обработка FloodWait, content_safety-гард на нишу.
    params: {channels: [ref...], niche, tone, acc_count, per_channel_delay}
    """
    from services import ai_comment_engine, account_manager, content_safety

    channels = [str(c).strip() for c in (params.get("channels") or []) if str(c).strip()]
    niche = (params.get("niche") or "").strip()
    tone = (params.get("tone") or "neutral").strip()
    acc_count = max(1, min(int(params.get("acc_count") or 3), 10))
    if not channels:
        return {"status": "failed", "summary": "⚠️ Не указаны каналы"}

    # Гард безопасности контента (ниша — единственный пользовательский текст;
    # сам коммент генерит LLM под постом, но нишу проверяем как контекст-инструкцию).
    verdict = await content_safety.enforce(pool, owner_id, niche or "comment", "", surface="ai_comment")
    if verdict.blocked:
        return {"status": "failed", "summary": "⚠️ Контент заблокирован политикой платформы"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, action_type="comment", respect_cooldown=True)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}
    # Риск-пульс: коммент в канал с флагнутого аккаунта = быстрый бан (fail-open).
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)
    accounts = accounts[:acc_count]

    channels = channels[:50]  # потолок против гигантских прогонов
    ok_count, fail_count = 0, 0
    total = len(channels)
    for idx, ref in enumerate(channels, 1):
        if await _is_cancelled(pool, op_id):
            break
        acc = accounts[idx % len(accounts)]
        try:
            res = await ai_comment_engine.post_ai_comment(
                acc["session_str"], dict(acc), ref, niche=niche, tone=tone)
            if res.get("ok"):
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok',$4)",
                    op_id, idx, ref, (res.get("comment") or "")[:200])
            else:
                fail_count += 1
                err = (res.get("error") or "")
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)", op_id, idx, ref, err[:200])
                if "FloodWait" in err or "flood" in err.lower():
                    await asyncio.sleep(30)
        except Exception as exc:
            fail_count += 1
            log.warning("ai_comment op=%d ref=%s: %s", op_id, ref, exc)
        await _safe_execute(pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            await asyncio.sleep(random.uniform(20, 45))  # органическая пауза между каналами

    summary = (f"💬 AI-комментинг\n✅ Успешно: {ok_count}/{total}"
               + (f"\n⚠️ Ошибок: {fail_count}" if fail_count else ""))
    return {"status": "done", "ok": ok_count, "failed": fail_count, "summary": summary}


async def _exec_compliance_scan(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Resource Compliance Scan: read-only проверка списка ресурсов на запрещённую
    тематику (CSAM/террор) через content_safety. Собирает доказательное досье —
    НЕ постит, НЕ жалуется, НЕ сносит (адресное действие оператор делает вручную,
    в т.ч. через strike_engine). Round-robin по аккаунтам, паузы между ресурсами.
    params: {resources: [ref...], per_resource_limit, acc_count}
    """
    from services import content_watch

    resources = [str(c).strip() for c in (params.get("resources") or []) if str(c).strip()]
    per_limit = max(1, min(int(params.get("per_resource_limit") or 50), 200))
    acc_count = max(1, min(int(params.get("acc_count") or 2), 10))
    if not resources:
        return {"status": "failed", "summary": "⚠️ Не указаны ресурсы для проверки"}

    accounts = await resource_selector.select_all_active(
        pool, owner_id, action_type="parse", respect_cooldown=True)
    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет доступных аккаунтов"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}
    accounts = accounts[:acc_count]

    resources = resources[:100]  # потолок против гигантских прогонов
    total = len(resources)
    await _safe_execute(pool, "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    flagged, clean_count, err_count = 0, 0, 0
    flagged_lines: list[str] = []
    for idx, ref in enumerate(resources, 1):
        if await _is_cancelled(pool, op_id):
            break
        acc = accounts[idx % len(accounts)]
        try:
            dossier = await content_watch.scan_resource(
                acc["session_str"], dict(acc), ref, limit=per_limit, low_risk=True)
            if not dossier.get("ok"):
                err_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)", op_id, idx, ref, (dossier.get("error") or "")[:200])
            elif dossier.get("verdict") == "prohibited":
                flagged += 1
                cats = ", ".join(dossier.get("categories") or [])
                first_hit = (dossier.get("hits") or [{}])[0]
                evidence = f"{first_hit.get('category_label','')}: «{first_hit.get('excerpt','')}»"
                flagged_lines.append(f"🚫 {dossier.get('resource', ref)} — {cats}")
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'flagged',$4)", op_id, idx, dossier.get("resource", ref),
                    (f"[{cats}] {evidence}")[:200])
            else:
                clean_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok',$4)", op_id, idx, dossier.get("resource", ref),
                    f"чисто ({dossier.get('scanned',0)} текстов)")
        except Exception as exc:
            err_count += 1
            log.warning("compliance_scan op=%d ref=%s: %s", op_id, ref, exc)
        await _safe_execute(pool, "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx < total:
            await asyncio.sleep(random.uniform(3, 8))  # мягкая пауза между read-only проверками

    head = (f"🛡️ Проверка на запрещёнку\n🚫 Найдено нарушений: {flagged}/{total}"
            f"\n✅ Чисто: {clean_count}" + (f"\n⚠️ Ошибок: {err_count}" if err_count else ""))
    if flagged_lines:
        head += "\n\n" + "\n".join(flagged_lines[:20])
        head += "\n\nℹ️ Досье в журнале операции. Жалобу подавайте адресно (раздел Strike)."
    return {"status": "done", "flagged": flagged, "clean": clean_count,
            "failed": err_count, "summary": head}


async def _exec_niche_growth_post(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Growth Agent: ищет группы в нише → вступает → постит рекламный текст.

    Безопасный режим:
    - Не более 5 групп за один запуск (снижает риск бана)
    - 3-8 минут между вступлением и постом (имитирует органическое поведение)
    - 10-20 минут между группами (Telegram не замечает паттерн спама)
    - Round-robin по аккаунтам с обработкой FloodWait
    """
    from services import niche_searcher, account_manager
    # Anti-detection: свой вариант рекламного текста в каждую группу (spintax; no-op
    # без него) — иначе один и тот же promo_text в 5 групп = сигнатура координации.
    from services.dm_engine import expand_spintax as _expand_spintax

    niche: str = params.get("niche", "")
    geo: str = (params.get("geo") or "").strip()
    promo_text: str = params.get("promo_text", "")
    # Безопасный лимит: не более 5 групп за запуск во избежание блокировки
    max_groups: int = min(int(params.get("max_groups") or 5), 5)
    # Сколько аккаунтов задействовать (0/не задано → безопасный дефолт 3)
    acc_count: int = max(1, min(int(params.get("acc_count") or 3), 10))
    # Гео дописывается к нише как доп. контекст для генерации ключевых слов —
    # search_niche_groups сам не умеет фильтровать по гео (Telegram SearchRequest
    # глобальный), но "фитнес Москва" вместо "фитнес" даёт более локальные ключи.
    search_description = f"{niche} {geo}".strip() if geo else niche

    if not niche or not promo_text:
        return {"status": "failed", "reason": "Нет ниши или рекламного текста"}

    # Universal content-safety backstop — Growth Agent searches for and joins
    # groups matching `niche`, then posts `promo_text` into them, so both fields
    # need the same CSAM/terrorism guard every other text-distribution surface has.
    from services import content_safety

    _verdict = await content_safety.enforce(
        pool, owner_id, niche, promo_text, surface="growth_agent"
    )
    if _verdict.blocked:
        return {"status": "failed", "reason": "Контент заблокирован политикой платформы"}

    # Выбираем прогретые аккаунты для постинга (запрошенное количество, дефолт 3)
    accounts = await resource_selector.select_accounts(pool, owner_id, acc_count, action_type="post")
    if not accounts:
        return {
            "status": "failed",
            "reason": "Нет активных аккаунтов",
            "summary": "❌ Нет активных аккаунтов для Growth Agent",
        }
    # Риск-пульс: не постим с аккаунтов под недавним серьёзным ограничением
    # (select_accounts учитывает flood/trust, но не единый is_account_quarantined).
    # fail-open: все в карантине → работаем всеми, чтобы не сорвать операцию.
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    # Берём лучший аккаунт для поиска групп
    search_acc = accounts[0]
    session_str = search_acc.get("session_str", "")
    if not session_str:
        return {"status": "failed", "reason": "Аккаунт без сессии"}

    # Отказной захват всего пула: дальше аккаунты выбираются из него по ходу
    # (в т.ч. запасной при карантине), поэтому захватываем пул целиком.
    claimed_ids = await try_claim_accounts([int(a["id"]) for a in accounts])
    if not claimed_ids:
        return {"status": "requeue",
                "reason": "Все аккаунты заняты другой операцией — попробуйте позже"}
    _busy = len(accounts) - len(claimed_ids)
    accounts = [a for a in accounts if int(a["id"]) in set(claimed_ids)]

    try:

        # Генерируем ключевые слова для ниши (+ гео, если задан)
        try:
            keywords = await niche_searcher.generate_keywords(search_description)
            log.info("niche_growth_post op_id=%d: keywords=%s", op_id, keywords)
        except Exception as exc:
            log.warning("niche_growth_post: keyword gen failed: %s", exc)
            keywords = [search_description]

        # Группы, в которые этот владелец уже заходил через Growth Agent раньше —
        # исключаем, чтобы повторные запуски не заходили и не постили повторно в
        # те же группы (лишний риск спам-флага без дополнительного охвата).
        try:
            prior_rows = await pool.fetch(
                "SELECT group_id FROM niche_growth_targets WHERE owner_id=$1", owner_id
            )
            exclude_ids = {int(r["group_id"]) for r in prior_rows}
        except Exception as e:
            log.warning('prior groups query failed: %s', e)
            exclude_ids = set()

        # Ищем группы
        try:
            groups = await niche_searcher.search_niche_groups(
                session_str,
                keywords,
                min_members=50,
                max_per_keyword=5,
                exclude_ids=exclude_ids,
                _acc=search_acc,
            )
        except Exception as exc:
            log.warning("niche_growth_post: group search failed: %s", exc)
            return {
                "status": "failed",
                "reason": f"Поиск групп не удался: {exc}",
                "summary": "❌ Не удалось найти группы в нише",
            }

        if not groups:
            # Correct total_items down from the submitted placeholder (bot handler
            # submits total_items=50 regardless of the real 5-group cap) so the
            # progress bar doesn't show a misleading "0/50" when nothing was found.
            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET total_items=0 WHERE id=$1", op_id
            )
            return {
                "status": "done",
                "ok": 0,
                "fail": 0,
                "summary": "⚠️ Групп по нише не найдено. Попробуйте уточнить описание.",
            }

        # Перемешиваем и берём только безопасный лимит
        random.shuffle(groups)
        groups = groups[:max_groups]
        total = len(groups)
        await _safe_execute(
                pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

        ok_count = 0
        err_count = 0
        acc_idx = 0

        for idx, grp in enumerate(groups):
            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": err_count,
                    "summary": f"Отменено. Опубликовано: {ok_count}/{total}",
                }

            # Round-robin по аккаунтам
            acc = accounts[acc_idx % len(accounts)]
            acc_idx += 1

            join_ref = grp.get("join_ref", "")
            grp_id = grp.get("id", 0)
            grp_title = grp.get("title", "")
            username = grp.get("username", "")
            access_hash = grp.get("access_hash", 0)

            # Шаг 1: Вступить в группу
            try:
                join_result = await account_manager.join_channel(
                    acc["session_str"], join_ref, _acc=acc
                )
                if "error" in join_result and not join_result.get("already_member"):
                    log.info(
                        "niche_growth_post: join failed grp=%s: %s",
                        join_ref, join_result.get("error"),
                    )
                    err_count += 1
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                    )
                    continue
            except Exception as exc:
                exc_str = str(exc).lower()
                if "floodwait" in exc_str or "flood" in exc_str:
                    # При FloodWait останавливаем текущий аккаунт
                    log.warning("niche_growth_post: FloodWait on join grp=%s: %s", join_ref, exc)
                    err_count += 1
                    await _safe_execute(
                            pool,
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                    )
                    # Большая пауза при флуде — переключиться на следующий аккаунт
                    await asyncio.sleep(random.uniform(300, 600))
                    continue
                log.warning("niche_growth_post: join exc grp=%s: %s", join_ref, exc)
                err_count += 1
                await _safe_execute(
                        pool,
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                continue

            # Запоминаем группу как уже посещённую — чтобы будущие запуски Growth
            # Agent для этого владельца не заходили и не постили сюда повторно.
            if grp_id:
                await _safe_execute(
                    pool,
                    "INSERT INTO niche_growth_targets(owner_id, group_id, title) "
                    "VALUES($1,$2,$3) ON CONFLICT (owner_id, group_id) DO NOTHING",
                    owner_id, grp_id, grp_title,
                )

            # Пауза после вступления перед постом: 3-8 минут (имитирует органичное поведение)
            join_to_post_delay = random.uniform(180, 480)
            log.debug("niche_growth_post: waiting %.0fs before posting to grp=%s", join_to_post_delay, join_ref)
            await asyncio.sleep(join_to_post_delay)

            if await _is_cancelled(pool, op_id):
                return {
                    "status": "cancelled",
                    "ok": ok_count,
                    "fail": err_count,
                    "summary": f"Отменено. Опубликовано: {ok_count}/{total}",
                }

            # Шаг 2: Опубликовать текст (свой spintax-вариант на эту группу)
            try:
                _promo = _expand_spintax(promo_text)
                post_result = await account_manager.post_to_channel(
                    acc["session_str"],
                    grp_id,
                    _promo,
                    access_hash=access_hash,
                    username=username,
                    _acc=acc,
                )
                if "error" in post_result:
                    log.info(
                        "niche_growth_post: post failed grp=%s title=%r: %s",
                        join_ref, grp_title, post_result.get("error"),
                    )
                    err_count += 1
                else:
                    ok_count += 1
                    log.info(
                        "niche_growth_post: posted to grp=%s title=%r msg_id=%s",
                        join_ref, grp_title, post_result.get("msg_id"),
                    )
            except Exception as exc:
                exc_str = str(exc).lower()
                if "floodwait" in exc_str or "flood" in exc_str:
                    log.warning("niche_growth_post: FloodWait on post grp=%s: %s", join_ref, exc)
                    await asyncio.sleep(random.uniform(300, 600))
                else:
                    log.warning("niche_growth_post: post exc grp=%s: %s", join_ref, exc)
                err_count += 1

            await _safe_execute(
                    pool,
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )

            # Пауза между группами: 10-20 минут (критично для безопасности).
            # Под глобальным губернатором: при давлении флота темп замедляется
            # (множитель ≥1 — спокойный флот не ускоряем, рисковый — тормозим).
            if idx < total - 1:
                between_groups_delay = await _governed_delay(
                    pool, owner_id, random.uniform(600, 1200))
                log.debug(
                    "niche_growth_post: waiting %.0fs before next group (%d/%d done)",
                    between_groups_delay, idx + 1, total,
                )
                await asyncio.sleep(between_groups_delay)

        summary = f"🌱 Growth Agent: ✅ {ok_count} ❌ {err_count} из {total} групп"
        return {
            "status": "done",
            "ok": ok_count,
            "fail": err_count,
            "groups_found": total,
            "summary": summary,
        }
    finally:
        await release_accounts(claimed_ids)


# ── Mini App handlers ─────────────────────────────────────────────────────────

async def _exec_account_warmup(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Запустить/обновить план прогрева аккаунта.
    params: {account_id, plan_type}
    """
    from services import account_warmer

    account_id = params.get("account_id")
    plan_type = params.get("plan_type", "standard")

    if not account_id:
        return {"status": "failed", "summary": "⚠️ account_id не указан"}

    try:
        account_id = int(account_id)
    except (ValueError, TypeError):
        return {"status": "failed", "summary": "⚠️ Неверный account_id"}

    # Verify account belongs to owner
    acc = await _safe_fetchrow(
            pool,
        "SELECT id, phone, first_name FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        account_id, owner_id,
    )
    if not acc:
        return {"status": "failed", "summary": "⚠️ Аккаунт не найден или не принадлежит вам"}

    name = acc.get("first_name") or acc.get("phone") or str(account_id)

    try:
        # If a plan already exists (created by the API before enqueuing this op),
        # do NOT reset current_day/started_at — just confirm and return.
        existing = await pool.fetchrow(
            "SELECT id, status FROM account_warmup_plans WHERE account_id=$1 AND owner_id=$2",
            account_id, owner_id,
        )
        if existing:
            # Раньше здесь рапортовалось «Прогрев активен» ЛЮБОМУ существующему
            # плану — включая отменённый и остановленный движком. Операция
            # сообщала об успехе, а прогрев не шёл: классический ложный успех.
            if (existing["status"] or "") == "active":
                return {
                    "status": "done",
                    "plan_id": existing["id"],
                    "summary": f"🌡️ Прогрев активен для {name} (план: {plan_type})",
                }
            await pool.execute(
                """UPDATE account_warmup_plans
                      SET status='active', started_at=now(),
                          pause_reason=NULL, pause_detail=NULL, paused_at=NULL,
                          pause_notified_at=NULL,
                          last_skip_reason=NULL, last_skip_at=NULL
                    WHERE id=$1""",
                existing["id"])
            return {
                "status": "done",
                "plan_id": existing["id"],
                "summary": f"🌡️ Прогрев возобновлён для {name} (план: {plan_type})",
            }
        # Plan not yet created (op triggered from bot handler or legacy path) — create it.
        plan_id = await account_warmer.create_warmup_plan(pool, owner_id, account_id, plan_type)
        return {
            "status": "done",
            "plan_id": plan_id,
            "summary": f"🌡️ Прогрев запущен для {name} (план: {plan_type}, план_id={plan_id})",
        }
    except Exception as exc:
        log.exception("_exec_account_warmup op=%d acc=%d", op_id, account_id)
        return {"status": "failed", "summary": f"⚠️ Ошибка запуска прогрева: {exc}"}


async def _exec_parse_audience(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Парсинг аудитории канала/группы.
    params: {source_ref, parse_type, limit}
    """
    from services import parser as _parser

    source_ref = (params.get("source_ref") or "").strip()
    parse_type = params.get("parse_type", "members")
    limit = int(params.get("limit") or 500)
    try:
        days_back = max(1, min(365, int(params.get("days_back") or 30)))
    except (TypeError, ValueError):
        days_back = 30

    if not source_ref:
        return {"status": "failed", "summary": "⚠️ source_ref не указан"}

    if limit < 1 or limit > 50000:
        limit = 500

    try:
        if parse_type == "active":
            result = await _parser.parse_active_users(
                pool, owner_id, source_ref, days_back=days_back, limit=limit
            )
        elif parse_type == "comments":
            result = await _parser.parse_commenters(
                pool, owner_id, source_ref, days_back=days_back, limit=limit
            )
        else:
            result = await _parser.parse_members(pool, owner_id, source_ref, limit=limit)

        total_found = result.get("total_found", 0)
        total_saved = result.get("total_saved", 0)
        status = result.get("status", "done")

        if status == "error":
            return {
                "status": "failed",
                "summary": f"⚠️ Парсинг {source_ref} не удался: {result.get('error', 'неизвестная ошибка')}",
            }

        await pool.execute(
            "UPDATE operation_queue SET total_items=$1, done_items=$2 WHERE id=$3",
            total_found, total_saved, op_id,
        )
        return {
            "status": "done",
            "total_found": total_found,
            "total_saved": total_saved,
            "summary": f"👥 Парсинг {source_ref}: найдено {total_found}, сохранено {total_saved}",
        }
    except Exception as exc:
        log.exception("_exec_parse_audience op=%d source=%s", op_id, source_ref)
        return {"status": "failed", "summary": f"⚠️ Ошибка парсинга: {exc}"}


async def _exec_reg_check(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Проверка даты регистрации пользователя / создания канала / группы.
    params: {target: "@username или ссылка"}
    """
    from services import registration_checker as _rc

    target = (params.get("target") or "").strip()
    if not target:
        return {"status": "failed", "summary": "⚠️ target не указан"}

    try:
        info = await _rc.get_entity_full_info(pool, owner_id, target)
        if not info:
            return {"status": "failed", "summary": f"⚠️ Не удалось получить данные для {target} (нет аккаунтов или объект не найден)"}

        await _rc.cache_result(pool, owner_id, info, info.get("name") or info.get("title"), info.get("username"))

        entity_type = info.get("entity_type", "unknown")
        name = info.get("name") or info.get("title") or target
        reg_date = info.get("exact_date") or info.get("date")
        method = info.get("method", "id_interpolation")

        date_str = reg_date.strftime("%d.%m.%Y") if reg_date else "неизвестно"
        return {
            "status": "done",
            "entity_type": entity_type,
            "name": name,
            "reg_date": reg_date.isoformat() if reg_date else None,
            "method": method,
            "summary": f"📅 {name} ({entity_type}): дата регистрации {date_str} [метод: {method}]",
        }
    except Exception as exc:
        log.exception("_exec_reg_check op=%d target=%s", op_id, target)
        return {"status": "failed", "summary": f"⚠️ Ошибка проверки: {exc}"}


async def _exec_ad_intel_scan(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Сканирование канала на рекламные посты.
    params: {channel: "username"}
    """
    from services import ad_intelligence as _ai

    channel = (params.get("channel") or "").strip().lstrip("@")
    if not channel:
        return {"status": "failed", "summary": "⚠️ channel не указан"}

    # Одна дверь: сырой `ORDER BY last_used` брал аккаунт, не спрашивая ни о
    # кулдауне, ни о статусе, ни о живости прокси — скан уходил забаненным или
    # обесточенным аккаунтом и падал. select_account_rotated идёт через
    # select_all_active (статусы, кулдаун, мёртвый прокси) и при этом сохраняет
    # смысл прежней сортировки — размазать нагрузку, а не долбить один аккаунт.
    try:
        from services import resource_selector as _rsel
        # action_type оставлен "default" СОЗНАТЕЛЬНО: у порога доверия внутри
        # стоит COALESCE(trust_score, 0), поэтому любой положительный порог
        # выкинул бы аккаунты с ещё не измеренным доверием (NULL) — на свежем
        # флоте операция стала бы «нет аккаунтов». Миграция добавляет только
        # защиту, а не новый способ отказать.
        acc = await _rsel.select_account_rotated(pool, owner_id, action_type="default")
        account_id = int(acc["id"]) if acc else 0
    except Exception as e:
        log.warning('resolve account_id failed: %s', e)
        account_id = 0

    if not account_id:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов для сканирования"}

    # Захват под живую сессию. Сессию поднимает не этот код, а
    # ad_intelligence.scan_channel_ads — по account_id, внутри себя. Косвенность
    # ничего не меняет: две сессии на одном auth-key дают AUTH_KEY_DUPLICATED.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    if not await _claim_available_accounts(op_id, [{"id": account_id}], owner_id):
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}

    try:
        result = await _ai.scan_channel_ads(pool, channel, account_id, owner_id)
        if result.get("status") == "error":
            return {"status": "failed", "summary": f"⚠️ Ошибка сканирования @{channel}: {result.get('error', 'неизвестная ошибка')}"}

        ad_posts = result.get("ad_posts_found", 0)
        return {
            "status": "done",
            "channel": channel,
            "ad_posts_found": ad_posts,
            "summary": f"🔍 Ad Intel @{channel}: найдено {ad_posts} рекламных постов",
        }
    except Exception as exc:
        log.exception("_exec_ad_intel_scan op=%d channel=%s", op_id, channel)
        return {"status": "failed", "summary": f"⚠️ Ошибка сканирования: {exc}"}


async def _exec_self_promo_blast(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Рассылка self-promo шаблона подписчикам управляемых ботов.
    params: {template_id: int}
    """
    import html as _html

    template_id = params.get("template_id")
    if not template_id:
        return {"status": "failed", "summary": "⚠️ template_id не указан"}

    try:
        tpl = await pool.fetchrow(
            "SELECT id, title, content, cta_text, cta_url FROM self_promo_templates "
            "WHERE id=$1 AND is_active=TRUE AND (owner_id=$2 OR owner_id IS NULL)",
            int(template_id), owner_id,
        )
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения шаблона: {exc}"}

    if not tpl:
        return {"status": "failed", "summary": "⚠️ Шаблон не найден или неактивен"}

    # Правильная реф-ссылка на системного бота (а не хардкод @BotMotherBot из сидов).
    correct_link = None
    try:
        from database import db as _db
        me = await bot.get_me()
        code = await _db.get_or_create_referral_code(pool, owner_id)
        if me and me.username:
            correct_link = f"https://t.me/{me.username}?start={code}"
    except Exception as exc:
        log.warning("self_promo_blast: не удалось построить реф-ссылку: %s", exc)

    def _fix_link(s):
        """Заменить хардкод-ссылки на BotMother на реальную реф-ссылку бота."""
        if not s or not correct_link:
            return s
        import re as _re
        return _re.sub(
            r"https?://t\.me/(?:BotMotherBot|botmother_bot|BotMother)\b[^\s\"<]*",
            correct_link, s, flags=_re.IGNORECASE,
        )

    content = _fix_link(tpl["content"])
    cta_url = _fix_link(tpl["cta_url"])
    # Если в шаблоне вообще нет ссылки — добавим реальную реф-ссылку как CTA
    if not cta_url and correct_link:
        cta_url = correct_link

    # Build message text
    text_parts = []
    if content:
        text_parts.append(content)
    if tpl["cta_text"] and cta_url:
        text_parts.append(f'\n<a href="{_html.escape(cta_url)}">{_html.escape(tpl["cta_text"])}</a>')
    elif tpl["cta_text"]:
        text_parts.append(f"\n{_html.escape(tpl['cta_text'])}")
    elif cta_url:
        text_parts.append(f"\n{_html.escape(cta_url)}")
    message_text = "\n".join(text_parts) or tpl["title"] or "Promo"

    # Get all active bot_users across owner's bots
    try:
        users = await pool.fetch(
            """SELECT bu.user_id, bu.bot_id, mb.token
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.is_active = TRUE AND mb.is_active = TRUE
               AND mb.token IS NOT NULL
               ORDER BY bu.user_id
               LIMIT 1000""",
            owner_id,
        )
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения подписчиков: {exc}"}

    if not users:
        return {"status": "done", "summary": "📢 Нет активных подписчиков для рассылки"}

    total = len(users)
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    ok_count = 0
    fail_count = 0
    # Group by bot_token to use the correct bot for each user
    token_cache: dict[int, Bot] = {}

    for idx, row in enumerate(users):
        if await _is_cancelled(pool, op_id):
            break
        user_id = row["user_id"]
        bot_id = row["bot_id"]
        token = row["token"]

        try:
            if bot_id not in token_cache:
                from aiogram import Bot as _Bot
                token_cache[bot_id] = _Bot(token=token)
            _b = token_cache[bot_id]
            await _b.send_message(user_id, message_text, parse_mode="HTML")
            ok_count += 1
        except Exception as exc:
            exc_s = str(exc).lower()
            if "blocked" in exc_s or "deactivated" in exc_s or "not found" in exc_s:
                try:
                    await pool.execute(
                        "UPDATE bot_users SET is_active=FALSE WHERE user_id=$1 AND bot_id=$2",
                        user_id, bot_id,
                    )
                except Exception as e:
                    log_exc_swallow(log, f"self_promo_blast: deactivate bot_user failed for user={user_id} bot={bot_id}: {e}")
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx % 20 == 0 and idx > 0:
            await asyncio.sleep(1)  # rate limit

    # Close bot instances
    for _b in token_cache.values():
        try:
            await _b.session.close()
        except Exception as e:
            log_exc_swallow(log, f"self_promo_blast: close bot session failed: {e}")

    await _safe_execute(
            pool,
        "UPDATE self_promo_templates SET use_count = COALESCE(use_count,0)+1 "
        "WHERE id=$1 AND (owner_id=$2 OR owner_id IS NULL)",
        int(template_id), owner_id,
    )
    return {
        "status": "done",
        "ok": ok_count,
        "fail": fail_count,
        "total": total,
        "summary": f"📢 Self-promo рассылка: ✅ {ok_count}/{total}" + (f" ❌ {fail_count}" if fail_count else ""),
    }


async def _exec_phone_check(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Проверка номеров телефонов через Telegram ImportContacts.
    params: {phones: list[str]}
    """
    from services import phone_checker_engine as _pce

    phones = params.get("phones") or []
    if not phones:
        return {"status": "failed", "summary": "⚠️ Список номеров пуст"}

    # Одна дверь. ImportContacts — одна из самых баноопасных операций Telegram,
    # а прежний сырой выбор брал аккаунт в кулдауне после флуда, в спамблоке или
    # на мёртвом прокси: проверка либо падала, либо загоняла аккаунт глубже.
    # action_type="default" — см. пояснение про COALESCE(trust_score, 0) в
    # _exec_ad_intel_scan: порог доверия выкинул бы аккаунты с NULL-доверием.
    try:
        from services import resource_selector as _rsel
        acc_row = await _rsel.select_account_rotated(pool, owner_id, action_type="default")
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения аккаунта: {exc}"}

    if not acc_row:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов для проверки"}

    # Захват под живую сессию: без него операция подключала бы аккаунт,
    # который в этот момент ведёт другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    _claimed = await _claim_available_accounts(op_id, [acc_row], owner_id)
    if not _claimed:
        return {"status": "requeue",
                "summary": "⏳ Аккаунт занят другой операцией — попробуйте позже"}
    acc_row = _claimed[0]

    acc = dict(acc_row)
    total = len(phones)
    registered = 0
    not_registered = 0
    errors = 0

    # Process in batches of 25 (Telegram limit)
    batch_size = 25
    for i in range(0, total, batch_size):
        if await _is_cancelled(pool, op_id):
            break
        batch = phones[i:i + batch_size]
        try:
            results = await _pce.check_phones_batch(acc["session_str"], acc, batch)
            for r in results:
                if r.get("registered") is True:
                    registered += 1
                elif r.get("registered") is False:
                    not_registered += 1
                else:
                    errors += 1
        except Exception as exc:
            log.warning("_exec_phone_check batch error: %s", exc)
            errors += len(batch)

        await _safe_execute(
                pool,
            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
        )
        if i + batch_size < total:
            await asyncio.sleep(3)  # Anti-flood

    return {
        "status": "done",
        "total": total,
        "registered": registered,
        "not_registered": not_registered,
        "errors": errors,
        "summary": f"📱 Проверено {total} номеров: ✅ {registered} зарегистрированы, ❌ {not_registered} нет" + (f", ⚠️ {errors} ошибок" if errors else ""),
    }


async def _exec_gift_scan(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Сканирование подарков во всех аккаунтах владельца.
    params: {}
    """
    from services import gift_inventory as _gi

    # Одна дверь: скан подарков поднимает сессию КАЖДОГО аккаунта, поэтому
    # забаненные и сидящие на мёртвом прокси давали гарантированную ошибку на
    # каждом — и портили счётчик исхода. action_type="default" — порог доверия
    # выкинул бы аккаунты с NULL-доверием (см. _exec_ad_intel_scan).
    try:
        from services import resource_selector as _rsel
        accounts = await _rsel.select_all_active(pool, owner_id, action_type="default")
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения аккаунтов: {exc}"}

    if not accounts:
        return {"status": "done", "summary": "📦 Нет активных аккаунтов для сканирования"}

    # Захват под живые сессии. Сессию каждого аккаунта поднимает
    # GiftInventoryService.scan_account_gifts по account_id — косвенно, но это
    # такая же живая сессия: без захвата аккаунт мог одновременно вести другую
    # операцию или прогрев (AUTH_KEY_DUPLICATED).
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    total_accounts = len(accounts)
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total_accounts, op_id)

    all_gifts: list[dict] = []
    accounts_ok = 0

    for idx, row in enumerate(accounts):
        if await _is_cancelled(pool, op_id):
            break
        account_id = row["id"]
        try:
            gifts = await _gi.GiftInventoryService.scan_account_gifts(pool, account_id, owner_id)
            all_gifts.extend(gifts)
            accounts_ok += 1
        except Exception as exc:
            log.warning("_exec_gift_scan account=%d: %s", account_id, exc)

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx + 1 < total_accounts:
            await asyncio.sleep(2)

    saved = 0
    if all_gifts:
        try:
            saved = await _gi.GiftInventoryService.sync_inventory_to_db(pool, owner_id, all_gifts)
        except Exception as exc:
            log.warning("_exec_gift_scan sync error: %s", exc)

    failed_accounts = total_accounts - accounts_ok
    return {
        "status": "done" if accounts_ok else "failed",
        "ok": accounts_ok,
        "failed": failed_accounts,
        "accounts_scanned": accounts_ok,
        "gifts_found": len(all_gifts),
        "gifts_saved": saved,
        "summary": f"🎁 Сканирование подарков: {accounts_ok}/{total_accounts} аккаунтов, найдено {len(all_gifts)} подарков",
    }


async def _exec_report_peer(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Репортинг пользователя/канала через несколько аккаунтов.
    params: {target: str, reason: str}
    total_items в operation_queue = acc_count (сколько аккаунтов использовать)
    """
    from services import reporter_engine as rep

    target = (params.get("target") or "").strip()
    reason = params.get("reason", "spam")
    if not target:
        return {"status": "failed", "summary": "⚠️ target не указан"}

    # acc_count from total_items
    try:
        total_items = await pool.fetchval(
            "SELECT total_items FROM operation_queue WHERE id=$1", op_id
        )
        acc_count = int(total_items or 5)
    except Exception as e:
        log.warning('account count query failed: %s', e)
        acc_count = 5

    # Одна дверь: репорт — действие, за которое аккаунт получает ограничения,
    # поэтому вести в него аккаунт в кулдауне или в спамблоке особенно дорого.
    # Срез до acc_count делаем ПОСЛЕ фильтров, иначе лимит выбирался бы из
    # непригодных. action_type="default" — см. пояснение в _exec_ad_intel_scan.
    try:
        from services import resource_selector as _rsel
        accounts = (await _rsel.select_all_active(
            pool, owner_id, action_type="default"))[:acc_count]
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения аккаунтов: {exc}"}

    if not accounts:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов для репортинга"}

    # Захват под живые сессии: без него операция подключала бы аккаунты,
    # которые в этот момент ведут другую операцию или прогрев — две сессии
    # на одном auth-key дают AUTH_KEY_DUPLICATED и убивают сессию.
    # Освобождение — централизованно в finally _run_op_task по op_id.
    accounts = await _claim_available_accounts(op_id, accounts, owner_id)
    if not accounts:
        return {"status": "requeue",
                "summary": "⏳ Все аккаунты заняты другими операциями — попробуйте позже"}

    # Риск-пульс: репорт с флагнутого аккаунта = быстрый бан этого аккаунта (fail-open).
    accounts, _ = await _filter_quarantined_accounts(pool, op_id, accounts)

    ok_count = 0
    fail_count = 0

    for idx, acc in enumerate(accounts):
        if await _is_cancelled(pool, op_id):
            break
        try:
            res = await rep.report_peer(dict(acc)["session_str"], dict(acc), target, reason)
            if res["ok"]:
                ok_count += 1
            else:
                fail_count += 1
                log.debug("report_peer acc=%d: %s", acc["id"], res.get("error"))
        except Exception as exc:
            log.warning("_exec_report_peer acc=%d: %s", acc["id"], exc)
            fail_count += 1

        await _safe_execute(
                pool,"UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
        if idx + 1 < len(accounts):
            await asyncio.sleep(2.5)

    return {
        "status": "done" if ok_count else "failed",
        "ok": ok_count,
        "failed": fail_count,
        "total": len(accounts),
        "summary": f"🚩 Репорт {target}: ✅ {ok_count}/{len(accounts)} успешно" + (f" ❌ {fail_count}" if fail_count else ""),
    }


async def _exec_auto_register(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Батч-регистрация Telegram-аккаунтов через SMS-сервис (5sim/sms-activate).
    params: {count:int, country:str}. Ключи SMS-сервиса берутся из platform_settings."""
    from bot.handlers.auto_registrar import _get_sms_client, _do_batch_register

    try:
        cnt = max(1, min(50, int(params.get("count") or 1)))
    except (TypeError, ValueError):
        cnt = 1
    country = str(params.get("country") or "RU")

    sms_client, service = await _get_sms_client(pool)
    if not sms_client:
        return {"status": "failed",
                "summary": f"⚠️ Не задан API-ключ SMS-сервиса ({service}). "
                           "Админ платформы задаёт ключ в настройках бота."}

    await pool.execute("UPDATE operation_queue SET total_items=$1 WHERE id=$2", cnt, op_id)

    async def _progress(i, ok, failed):
        try:
            await pool.execute(
                "UPDATE operation_queue SET done_items=$1 WHERE id=$2", ok + failed, op_id)
        except Exception as e:
            log.warning("auto_register progress update failed for op %d: %s", op_id, e)
        return not await _is_cancelled(pool, op_id)

    try:
        res = await _do_batch_register(pool, owner_id, country, cnt, sms_client, None,
                                       progress_cb=_progress)
    except Exception as exc:
        log.exception("auto_register op=%d owner=%d", op_id, owner_id)
        return {"status": "failed", "summary": f"⚠️ Ошибка авторегистрации: {str(exc)[:120]}"}

    ok_n = len(res.get("ok", []))
    fail_n = len(res.get("failed", []))
    await pool.execute("UPDATE operation_queue SET done_items=$1 WHERE id=$2", ok_n + fail_n, op_id)
    return {
        "status": "done" if ok_n else "failed",
        "ok": ok_n,
        "failed": fail_n,
        "summary": f"📱 Авторег {country}: ✅ {ok_n}/{cnt}" + (f" ⚠️ {fail_n}" if fail_n else ""),
    }


async def _exec_leave_all_chats(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выход из всех чатов/групп аккаунта.
    params: {account_id: int}
    """
    from services import account_manager

    acc, err = await _claim_single_account(pool, owner_id, params)
    if err:
        return err
    account_id = int(acc["id"])

    client = account_manager._make_client(acc["session_str"], dict(acc))
    left = 0
    failed = 0
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        # Use high-level get_dialogs() which resolves entities automatically.
        # Raw GetDialogsRequest returns Dialog TL types without .entity attribute,
        # which caused the list comprehension to always produce an empty chats list.
        # wait_for: мёртвый прокси/half-open сокет иначе подвешивает операцию навсегда.
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=200), timeout=60)
        chats = [d.entity for d in dialogs if d.is_group or d.is_channel]
        total = len(chats)
        await pool.execute("UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)
        for entity in chats:
            if await _is_cancelled(pool, op_id):
                break
            try:
                await asyncio.wait_for(client.delete_dialog(entity), timeout=30)
                left += 1
            except Exception as exc:
                log.debug("leave_all_chats: skip %s: %s", getattr(entity, "id", "?"), exc)
                failed += 1
            await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            await asyncio.sleep(1.5)
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, f"leave_all_chats: client disconnect failed for acc {account_id}: {e}")
        await release_accounts([account_id])

    return {
        "status": "done",
        "left": left,
        "failed": failed,
        "summary": f"🚪 Выход из чатов: ✅ {left} успешно" + (f" ❌ {failed}" if failed else ""),
    }


async def _exec_read_all_dialogs(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Пометить все диалоги аккаунта прочитанными (send_read_acknowledge).
    Естественное «дочитывание» непрочитанного — снижает риск-сигналы у аккаунта.
    params: {account_id: int}
    """
    from services import account_manager

    acc, err = await _claim_single_account(pool, owner_id, params)
    if err:
        return err
    account_id = int(acc["id"])

    client = account_manager._make_client(acc["session_str"], dict(acc))
    read = 0
    failed = 0
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        # wait_for: мёртвый прокси/half-open сокет иначе подвешивает операцию навсегда.
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=300), timeout=60)
        # Только непрочитанные — не трогаем то, что уже прочитано.
        unread = [d for d in dialogs if getattr(d, "unread_count", 0)]
        total = len(unread)
        await pool.execute("UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)
        for d in unread:
            if await _is_cancelled(pool, op_id):
                break
            try:
                await asyncio.wait_for(client.send_read_acknowledge(d.entity), timeout=30)
                read += 1
            except Exception as exc:
                log.debug("read_all_dialogs: skip %s: %s", getattr(d, "id", "?"), exc)
                failed += 1
            await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            await asyncio.sleep(0.8)
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, f"read_all_dialogs: client disconnect failed for acc {account_id}: {e}")
        await release_accounts([account_id])

    return {
        "status": "done",
        "read": read,
        "failed": failed,
        "summary": f"📖 Прочитано диалогов: ✅ {read}" + (f" ❌ {failed}" if failed else ""),
    }


async def _exec_delete_private_dialogs(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Удалить приватные (личные) диалоги аккаунта — очистка списка ЛС.
    Комплемент к leave_all_chats (тот уходит из групп/каналов). delete_dialog по
    личке удаляет переписку у себя. params: {account_id: int}.
    """
    from services import account_manager

    acc, err = await _claim_single_account(pool, owner_id, params)
    if err:
        return err
    account_id = int(acc["id"])

    client = account_manager._make_client(acc["session_str"], dict(acc))
    deleted = 0
    failed = 0
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=300), timeout=60)
        # ТОЛЬКО личные диалоги (is_user) — группы/каналы за leave_all_chats.
        private = [d for d in dialogs if getattr(d, "is_user", False)]
        total = len(private)
        await pool.execute("UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)
        for d in private:
            if await _is_cancelled(pool, op_id):
                break
            try:
                await asyncio.wait_for(client.delete_dialog(d.entity), timeout=30)
                deleted += 1
            except Exception as exc:
                log.debug("delete_private_dialogs: skip %s: %s", getattr(d, "id", "?"), exc)
                failed += 1
            await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            await asyncio.sleep(1.0)
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, f"delete_private_dialogs: client disconnect failed for acc {account_id}: {e}")
        await release_accounts([account_id])

    return {
        "status": "done",
        "deleted": deleted,
        "failed": failed,
        "summary": f"🗑 Удалено личных диалогов: ✅ {deleted}" + (f" ❌ {failed}" if failed else ""),
    }


async def _exec_delete_contacts(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Удаление всех контактов аккаунта.
    params: {account_id: int}
    """
    from services import account_manager
    from telethon.tl.functions.contacts import GetContactsRequest, DeleteContactsRequest

    acc, err = await _claim_single_account(pool, owner_id, params)
    if err:
        return err
    account_id = int(acc["id"])

    client = account_manager._make_client(acc["session_str"], dict(acc))
    deleted = 0
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        contacts = await asyncio.wait_for(client(GetContactsRequest(hash=0)), timeout=30)
        users = getattr(contacts, "users", [])
        total = len(users)
        await pool.execute("UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)
        if users:
            try:
                await asyncio.wait_for(client(DeleteContactsRequest(id=users)), timeout=60)
                deleted = total
            except Exception as exc:
                log.warning("delete_contacts bulk failed: %s — trying one by one", exc)
                for u in users:
                    if await _is_cancelled(pool, op_id):
                        break
                    try:
                        await client(DeleteContactsRequest(id=[u]))
                        deleted += 1
                    except Exception as e:
                        log_exc_swallow(log, f"delete_contacts: single contact delete failed for user {getattr(u, 'id', '?')}: {e}")
                    await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
                    await asyncio.sleep(0.5)
        await pool.execute("UPDATE operation_queue SET done_items=$1 WHERE id=$2", deleted, op_id)
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, f"delete_contacts: client disconnect failed for acc {account_id}: {e}")
        await release_accounts([account_id])

    return {
        "status": "done",
        "deleted": deleted,
        "summary": f"🗑 Удаление контактов: ✅ {deleted} из {total if users else 0} удалено",
    }


async def _exec_run_broadcast(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Запуск рассылки через broadcaster для одного бота.
    params: {bot_id: int, broadcast_id: int, text: str}
    """
    from services import broadcaster

    bot_id = params.get("bot_id")
    broadcast_id = params.get("broadcast_id")
    text = (params.get("text") or "").strip()
    buttons = params.get("buttons") or None
    silent = bool(params.get("silent"))
    segment = str(params.get("segment") or "all")
    _seg_sql = {
        "active_7d": " AND last_seen >= now() - interval '7 days'",
        "active_30d": " AND last_seen >= now() - interval '30 days'",
    }.get(segment, "")

    if not bot_id or not text:
        return {"status": "failed", "summary": "⚠️ bot_id и text обязательны"}

    try:
        bot_row = await pool.fetchrow(
            "SELECT token, bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            int(bot_id), owner_id,
        )
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения бота: {exc}"}

    if not bot_row:
        return {"status": "failed", "summary": "⚠️ Бот не найден"}

    # Явный список получателей (напр. повторная отправка недоставленным) —
    # если передан, используем его; иначе тянем аудиторию по сегменту.
    explicit_ids = params.get("user_ids")
    try:
        if isinstance(explicit_ids, list) and explicit_ids:
            user_ids = [int(x) for x in explicit_ids]
        else:
            user_ids = [r["user_id"] for r in await pool.fetch(
                "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE" + _seg_sql, int(bot_id)
            )]
    except Exception as exc:
        return {"status": "failed", "summary": f"⚠️ Ошибка получения подписчиков: {exc}"}

    if not user_ids:
        return {"status": "done", "summary": "📭 Нет активных подписчиков для рассылки"}

    total = len(user_ids)
    await _safe_execute(
            pool,"UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id)

    # Create/reuse broadcast record
    if not broadcast_id:
        from database import db as _db
        broadcast_id = await _db.create_broadcast(pool, int(bot_id), text, total, owner_id, buttons=buttons, silent=silent)
    elif _seg_sql:
        # Сегментная рассылка: сохраняем целевой список, иначе resume после
        # рестарта отправит всей аудитории бота.
        try:
            await _safe_execute(pool,
                "UPDATE broadcasts SET target_user_ids=$1::jsonb WHERE id=$2",
                json.dumps(user_ids), broadcast_id)
        except Exception:
            log.warning("run_broadcast: не удалось сохранить target_user_ids для сегмента op=%s", op_id)

    broadcaster.start(pool, None, broadcast_id, bot_row["token"], int(bot_id), text, None, user_ids, buttons, silent=silent)
    await _safe_execute(
            pool,"UPDATE operation_queue SET done_items=$1 WHERE id=$2", total, op_id)

    return {
        "status": "done",
        "broadcast_id": broadcast_id,
        "total": total,
        "summary": f"📢 Рассылка запущена: {total} получателей",
    }


async def _exec_clone_adapt(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Клонирование профиля бота: имя/описание/фото/команды.
    params: {source_bot_id: int, target_bot_id: int, fields: str (comma-sep)}
    """
    import aiohttp as _aio
    from services import bot_api as _bapi

    source_bot_id = params.get("source_bot_id")
    target_bot_id = params.get("target_bot_id")
    fields_str = str(params.get("fields", "name,desc"))
    fields = [f.strip() for f in fields_str.split(",") if f.strip()]

    if not source_bot_id or not target_bot_id or not fields:
        return {"status": "failed", "summary": "⚠️ Неверные параметры clone_adapt"}

    src_row = await _safe_fetchrow(
            pool,
        "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        int(source_bot_id), owner_id,
    )
    tgt_row = await _safe_fetchrow(
            pool,
        "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        int(target_bot_id), owner_id,
    )
    if not src_row:
        return {"status": "failed", "summary": "⚠️ Исходный бот не найден"}
    if not tgt_row:
        return {"status": "failed", "summary": "⚠️ Целевой бот не найден"}

    src_token = src_row["token"]
    tgt_token = tgt_row["token"]
    src_name = src_row["username"] or src_row["first_name"] or f"id{source_bot_id}"
    tgt_name = tgt_row["username"] or tgt_row["first_name"] or f"id{target_bot_id}"

    errors = []
    ok_count = 0

    async with _aio.ClientSession() as http:
        if "name" in fields:
            me = await _bapi.get_my_name(http, src_token)
            src_display_name = me.get("name", "") if me else ""
            if src_display_name and await _bapi.set_name(http, tgt_token, src_display_name):
                ok_count += 1
            else:
                errors.append("имя")

        if "desc" in fields:
            d = await _bapi.get_my_description(http, src_token)
            src_desc = d.get("description", "") if d else ""
            if await _bapi.set_description(http, tgt_token, src_desc):
                ok_count += 1
            else:
                errors.append("описание")

        if "short" in fields:
            d = await _bapi.get_my_short_description(http, src_token)
            src_short = d.get("short_description", "") if d else ""
            if await _bapi.set_short_description(http, tgt_token, src_short):
                ok_count += 1
            else:
                errors.append("краткое описание")

        if "commands" in fields:
            cmds = await _bapi.get_my_commands(http, src_token)
            if cmds is not None and await _bapi.set_my_commands(http, tgt_token, cmds):
                ok_count += 1
            else:
                errors.append("команды")

    detail = f"Ошибки: {', '.join(errors)}" if errors else f"OK ({ok_count} полей)"
    status = "failed" if errors and ok_count == 0 else "done"

    await _safe_execute(
            pool,
        """INSERT INTO clone_adapt_history (owner_id, source_bot_id, target_bot_id, fields, status, details)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        owner_id, int(source_bot_id), int(target_bot_id),
        fields_str, "ok" if status == "done" else "error", detail,
    )

    return {
        "status": status,
        "summary": f"🔄 Клон @{src_name} → @{tgt_name}: {detail}",
    }
