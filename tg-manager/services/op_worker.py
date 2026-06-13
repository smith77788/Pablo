"""Фоновый воркер для выполнения очереди операций (параллельный режим)."""

import asyncio
import json
import logging
import random
import re
import time
import aiohttp
import asyncpg
from aiogram import Bot
from database import db
from services.logger import log_exc_swallow
from bot.utils.op_helpers import extract_flood_wait
from services import resource_selector
from services import infra_memory as _infra_mem

log = logging.getLogger(__name__)
_POLL_INTERVAL = 10  # секунд между проверками очереди
_STALE_RUNNING_TIMEOUT_MIN = 60  # операции в running > N минут → reset в pending
_MAX_PARALLEL = 8  # максимум параллельных операций глобально
_MAX_PARALLEL_PER_OWNER = 3  # максимум на одного владельца (далее в коде)

# Реестр аккаунтов, занятых активными операциями op_worker.
# account_warmer проверяет этот реестр перед использованием аккаунта.
_accounts_in_use: set[int] = set()
_operation_account_locks: dict[int, set[int]] = {}
_accounts_lock = asyncio.Lock()

# Опциональный пул БД для персистентных обновлений in_operation.
# Устанавливается через init_op_worker_pool() при старте.
_db_pool: "asyncpg.Pool | None" = None


def init_op_worker_pool(pool: "asyncpg.Pool") -> None:
    """Вызывать один раз при старте, чтобы mark/release синхронизировались с БД."""
    global _db_pool
    _db_pool = pool


async def reset_stale_in_operation(pool: "asyncpg.Pool") -> None:
    """Сбросить все зависшие in_operation=TRUE после рестарта бота."""
    try:
        await pool.execute("UPDATE tg_accounts SET in_operation = FALSE WHERE in_operation = TRUE")
        log.info("op_worker: stale in_operation flags cleared")
    except Exception as e:
        log.warning("op_worker: failed to clear stale in_operation: %s", e)


def _fire_db_flag(acc_ids: list[int], value: bool) -> None:
    """Fire-and-forget DB update for in_operation flag (best-effort)."""
    if not _db_pool or not acc_ids:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_db_flag(acc_ids, value))
    except RuntimeError:
        pass  # No running loop (called from sync context) — skip DB update
    except Exception:
        pass


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


async def mark_accounts_in_use(acc_ids: list[int]) -> None:
    """Пометить аккаунты как занятые op_worker-операцией."""
    async with _accounts_lock:
        _accounts_in_use.update(acc_ids)
    _fire_db_flag(acc_ids, True)


async def release_accounts(acc_ids: list[int]) -> None:
    """Освободить аккаунты после завершения операции."""
    async with _accounts_lock:
        for aid in acc_ids:
            _accounts_in_use.discard(aid)
            for locked_acc_ids in _operation_account_locks.values():
                locked_acc_ids.discard(aid)
    _fire_db_flag(acc_ids, False)


async def release_operation_accounts(op_id: int) -> None:
    """Release every account claimed by an operation after executor errors."""
    freed: list[int] = []
    async with _accounts_lock:
        acc_ids = _operation_account_locks.pop(op_id, set())
        for aid in acc_ids:
            _accounts_in_use.discard(aid)
            freed.append(aid)
    if freed:
        _fire_db_flag(freed, False)


async def _claim_available_accounts(op_id: int, accounts: list) -> list:
    """Atomically claim free accounts and bind them to operation cleanup."""
    async with _accounts_lock:
        claimed = [a for a in accounts if int(a["id"]) not in _accounts_in_use]
        acc_ids = [int(a["id"]) for a in claimed]
        _accounts_in_use.update(acc_ids)
        if acc_ids:
            _operation_account_locks.setdefault(op_id, set()).update(acc_ids)
    if claimed:
        _fire_db_flag([int(a["id"]) for a in claimed], True)
    return claimed


def is_account_in_use(acc_id: int) -> bool:
    """Проверить занят ли аккаунт (non-async, читает snapshot)."""
    return acc_id in _accounts_in_use


# ── Retry Intelligence ─────────────────────────────────────────────────────────

_RETRYABLE_ERRORS = {
    "TimeoutError",
    "ConnectionError",
    "NetworkError",
    "ConnectionResetError",
    "ServerError",
    "OSError",
    "asyncio.TimeoutError",
    "TelegramNetworkError",
}
_FATAL_ERRORS = {
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
    "UserDeactivatedBan",
    "UserDeactivatedError",
    "BotKicked",
    "PhoneNumberBanned",
    "UserBannedInChannel",
    "ChannelBannedError",
    "ChatWriteForbiddenError",
}
# Fatal message fragments — if present in exception message, never retry
_FATAL_MSG_PATTERNS = re.compile(
    r"USER_DEACTIVATED|ACCOUNT_BANNED|USER_BANNED|SESSION_PASSWORD_NEEDED|"
    r"AUTH_KEY_DUPLICATED|PHONE_NUMBER_BANNED|BOT_KICKED|CHANNEL_BANNED|"
    r"permanently banned|account is banned",
    re.IGNORECASE,
)
_FLOOD_PATTERNS = re.compile(r"flood.wait|FLOOD_WAIT|FloodWait", re.IGNORECASE)
_PEER_FLOOD_PATTERNS = re.compile(r"peer.flood|PEER_FLOOD|PeerFlood", re.IGNORECASE)
_NETWORK_PATTERNS = re.compile(
    r"connection to telegram failed|general socks server failure|proxy недоступен|timeout при подключении|ошибка сети",
    re.IGNORECASE,
)


def _normalize_result(result: dict, op_type: str, duration_s: float) -> dict:
    """Обеспечить единый формат результата операции для хранения и отчётов.

    Канонические поля: status, ok, failed, total, summary, duration_s.
    Существующие алиасы (sent, created) нормализуются в ok.
    """
    if not isinstance(result, dict):
        result = {"status": "done", "summary": str(result)}

    # Нормализация ok: разные exec-функции используют sent/ok/created
    if "ok" not in result:
        for alias in ("sent", "created", "waves_completed"):
            if alias in result:
                result["ok"] = result[alias]
                break
        else:
            result["ok"] = 0

    if "failed" not in result:
        result["failed"] = 0

    if "total" not in result:
        result["total"] = result.get("ok", 0) + result.get("failed", 0)

    if "summary" not in result or not result["summary"]:
        ok = result.get("ok", 0)
        failed = result.get("failed", 0)
        result["summary"] = f"✅ {ok} успешно, ❌ {failed} ошибок"

    result["duration_s"] = round(duration_s, 1)
    result["op_type"] = op_type
    return result


def _classify_op_error(exc: Exception) -> str:
    """Классифицирует ошибку операции: 'retry' | 'flood' | 'fatal' | 'skip'."""
    name = type(exc).__name__
    msg = str(exc)
    # Fatal: known class names OR fatal message patterns — do NOT retry these
    if (
        name in _FATAL_ERRORS
        or "SESSION_REVOKED" in msg
        or "AUTH_KEY" in msg
        or _FATAL_MSG_PATTERNS.search(msg)
    ):
        return "fatal"
    if _PEER_FLOOD_PATTERNS.search(msg) or _PEER_FLOOD_PATTERNS.search(name):
        return "peer_flood"
    if _FLOOD_PATTERNS.search(msg) or _FLOOD_PATTERNS.search(name):
        return "flood"
    if (
        name in _RETRYABLE_ERRORS
        or "timeout" in msg.lower()
        or "connection" in msg.lower()
    ):
        return "retry"
    if (
        "CHANNEL_PRIVATE" in msg
        or "CHAT_ADMIN_REQUIRED" in msg
        or "ChatAdminRequired" in name
    ):
        return "skip"
    return "retry"


def _is_network_or_proxy_error(error_text: str) -> bool:
    return bool(_NETWORK_PATTERNS.search(error_text))


async def _record_network_isolation(
    pool: asyncpg.Pool,
    account_id: int,
    action_type: str,
    operation_id: int,
    error_text: str,
    cooldown_s: int = 15 * 60,
) -> None:
    await pool.execute(
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
    )
    try:
        from services import account_health

        account_health.update_after_failure(account_id, action_type, is_flood=False)
    except Exception:
        log_exc_swallow(
            log,
            f"op_worker: account_health network isolation failed for account_id={account_id}",
        )
    await pool.execute(
        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,0,$2,'error',$3)",
        operation_id,
        str(account_id),
        f"Аккаунт изолирован на {cooldown_s}s из-за сетевого/прокси сбоя: {error_text[:160]}",
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
    """
    kind = _classify_op_error(exc)
    if kind in ("fatal", "skip"):
        return False

    row = await pool.fetchrow(
        "SELECT retry_count, max_retries FROM operation_queue WHERE id=$1", op_id
    )
    if not row:
        return False
    retry_count = (row["retry_count"] or 0) + 1
    max_retries = row["max_retries"] or 3

    if retry_count > max_retries:
        return False

    flood_wait = extract_flood_wait(exc, str(exc))
    if kind == "peer_flood":
        backoff = 48 * 3600
    elif kind == "flood" and flood_wait > 0:
        backoff = min(flood_wait + 60, 24 * 3600)
    else:
        backoff = min(30 * (2 ** (retry_count - 1)), 600)

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

    await pool.execute(
        """UPDATE operation_queue
            SET status='pending',
                retry_count=$1,
                last_error=$2,
                scheduled_for=now() + ($4 * interval '1 second'),
                started_at=NULL
            WHERE id=$3""",
        retry_count,
        str(exc)[:300],
        op_id,
        backoff,
    )
    log.info(
        "op_worker: op %d queued for retry %d/%d in %ds",
        op_id,
        retry_count,
        max_retries,
        backoff,
    )
    return True


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
_active_lock = asyncio.Lock()

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

# Cache for _is_cancelled: op_id -> (result: bool, checked_at: float)
_cancel_cache: dict[int, tuple[bool, float]] = {}
_CANCEL_CACHE_TTL = 5.0  # seconds between DB checks in tight loops


async def _progress_monitor(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, op_type: str
) -> None:
    """Периодически проверяет прогресс и уведомляет на 25/50/75% (один раз каждый milestone)."""
    _progress_milestones[op_id] = 0
    try:
        while True:
            await asyncio.sleep(15)
            try:
                row = await pool.fetchrow(
                    "SELECT total_items, done_items, status FROM operation_queue WHERE id=$1",
                    op_id,
                )
                if not row or row["status"] != "running":
                    break
                total = row["total_items"] or 0
                done = row["done_items"] or 0
                if total <= 0:
                    continue
                pct = int(done * 100 / total)

                last = _progress_milestones.get(op_id, 0)
                milestone = None
                for m in (25, 50, 75):
                    if pct >= m > last:
                        milestone = m
                        break
                if milestone is None:
                    continue

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
                    pool,
                    bot,
                    owner_id,
                    "op_complete",
                    f"⏳ <b>Операция #{op_id}</b> — {milestone}%\n"
                    f"[{bar}] {done}/{total}\n"
                    f"<code>{op_type}</code>",
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


async def _reset_stale_running(pool: asyncpg.Pool) -> None:
    """При старте воркера сбрасывает операции в статусе 'running' обратно в 'pending'.

    Нужно после SIGTERM/рестарта: операции могли оказаться в running-состоянии
    без реально работающей задачи. Сбрасываем их с очисткой started_at, чтобы
    они были подхвачены воркером заново.
    """
    result = await pool.execute(
        """UPDATE operation_queue
           SET status = 'pending', started_at = NULL
           WHERE status = 'running'""",
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
    """Периодически сбрасывает 'running' операции, которые висят дольше N минут."""
    try:
        result = await pool.execute(
            f"""UPDATE operation_queue
                SET status = 'pending', started_at = NULL
                WHERE status = 'running'
                  AND started_at < now() - INTERVAL '{_STALE_RUNNING_TIMEOUT_MIN} minutes'""",
        )
        count = int((result or "UPDATE 0").split()[-1])
        if count:
            log.warning("op_worker watchdog: reset %d stale running ops → pending", count)
    except Exception as e:
        log_exc_swallow(log, f"op_worker watchdog error: {e}")


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Запускается как asyncio.create_task(op_worker.run(pool, bot)) в main.py."""
    log.info("Operation worker started (parallel mode, max=%d)", _MAX_PARALLEL)
    await _reset_stale_running(pool)
    _watchdog_tick = 0
    while True:
        try:
            await _process_pending(pool, bot)
        except Exception as e:
            log.exception("op_worker error: %s", e)
        _watchdog_tick += 1
        # Run stale-running watchdog every 6 poll cycles (~1 minute)
        if _watchdog_tick % 6 == 0:
            await _watchdog_stale(pool)
        await asyncio.sleep(_POLL_INTERVAL)


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
    row = await pool.fetchrow("SELECT status FROM operation_queue WHERE id=$1", op_id)
    result = bool(row and row["status"] == "cancelled")
    _cancel_cache[op_id] = (result, now)
    return result


async def _process_pending(pool: asyncpg.Pool, bot: Bot) -> None:
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
    rows = await pool.fetch(
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
        asyncio.create_task(_run_op_task(pool, bot, dict(row)))


async def _run_op_task(pool: asyncpg.Pool, bot: Bot, row: dict) -> None:
    """Запустить одну операцию в отдельной asyncio-задаче."""
    op_id = row["id"]
    owner_id = row["owner_id"]
    op_type = row["op_type"]
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
                await db.notify_if_enabled(
                    pool,
                    bot,
                    owner_id,
                    "op_complete",
                    f"⚙️ <b>Операция #{op_id}</b> запущена: <code>{op_type}</code>",
                    reply_markup=start_kb.as_markup(),
                )
            except Exception:
                log_exc_swallow(
                    log, f"Сбой отправки уведомления о запуске операции #{op_id}"
                )

            # Запустить фоновый монитор прогресса для длинных операций
            progress_task = asyncio.create_task(
                _progress_monitor(pool, bot, op_id, owner_id, op_type)
            )

            if op_type == "mass_publish":
                result = await _exec_mass_publish(pool, bot, op_id, owner_id, params)
            elif op_type == "bulk_bot_edit":
                result = await _exec_bulk_bot_edit(pool, bot, op_id, owner_id, params)
            elif op_type == "bulk_join":
                result = await _exec_bulk_join(pool, bot, op_id, owner_id, params)
            elif op_type == "bulk_leave":
                result = await _exec_bulk_leave(pool, bot, op_id, owner_id, params)
            elif op_type == "global_presence_channel":
                result = await _exec_global_presence_channel(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type == "global_presence_group":
                result = await _exec_global_presence_channel(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type == "global_presence_bot":
                result = await _exec_global_presence_bot(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type == "bulk_create_channels":
                result = await _exec_bulk_create_channels(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type == "bot_factory":
                result = await _exec_bot_factory(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type in ("global_presence_full_package", "global_presence_package"):
                result = await _exec_global_presence_channel(
                    pool, bot, op_id, owner_id, params
                )
            elif op_type == "strike":
                result = await _exec_strike(pool, bot, op_id, owner_id, params)
            elif op_type == "gift_transfer":
                from services.gift_operation import _exec_gift_transfer

                result = await _exec_gift_transfer(pool, op_id, params)
            elif op_type == "dm_campaign":
                result = await _exec_dm_campaign(pool, bot, op_id, owner_id, params)
            elif op_type == "network_broadcast":
                result = await _exec_network_broadcast(pool, bot, op_id, owner_id, params)
            elif op_type == "seed_presence_pack":
                result = await _exec_seed_presence_pack(pool, bot, op_id, owner_id, params)
            else:
                log.warning(
                    "op_worker: unknown op_type=%r for op_id=%s owner_id=%s — marking done/skipped",
                    op_type,
                    op_id,
                    owner_id,
                )
                result = {
                    "status": "skipped",
                    "reason": f"unknown op_type: {op_type}",
                    "summary": f"⚠️ Неизвестный тип операции: {op_type}",
                }

            # Не перезаписывать статус если операция была отменена в процессе
            if result.get("status") == "cancelled":
                await pool.execute(
                    "UPDATE operation_queue SET status='cancelled', finished_at=now() "
                    "WHERE id=$1 AND status NOT IN ('done','failed','cancelled')",
                    op_id,
                )
                return

            current = await pool.fetchrow(
                "SELECT status FROM operation_queue WHERE id=$1", op_id
            )
            if current and current["status"] == "cancelled":
                await pool.execute(
                    "UPDATE operation_queue SET finished_at=now() "
                    "WHERE id=$1 AND status='cancelled' AND finished_at IS NULL",
                    op_id,
                )
                return

            elapsed = time.monotonic() - _t_start
            duration_seconds = round(elapsed, 1)
            result = _normalize_result(result, op_type, duration_seconds)
            log.info(
                "op_worker: op_id=%d op_type=%s done in %.1fs (duration_seconds=%.1f) — %s",
                op_id,
                op_type,
                elapsed,
                duration_seconds,
                result.get("summary", ""),
            )
            await pool.execute(
                "UPDATE operation_queue SET status='done', finished_at=now(), result=$1::jsonb WHERE id=$2",
                json.dumps(result, ensure_ascii=False),
                op_id,
            )
            # Audit trail: write operation completion to operation_audit
            _op_summary = result.get("summary", "")
            _acc_ids_done = params.get("account_ids") or []
            if _acc_ids_done:
                for _audit_acc_id in _acc_ids_done:
                    await _audit(
                        pool,
                        owner_id,
                        op_type,
                        "success",
                        operation_id=op_id,
                        account_id=int(_audit_acc_id),
                        duration_ms=int(duration_seconds * 1000),
                    )
            else:
                await _audit(
                    pool,
                    owner_id,
                    op_type,
                    "success",
                    operation_id=op_id,
                    duration_ms=int(duration_seconds * 1000),
                )
            summary = _op_summary
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from bot.callbacks import BmCb, StrikeCb

            kb = InlineKeyboardBuilder()
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
            _notify_header = f"✅ <b>Операция #{op_id}</b> завершена за {duration_seconds}с\n"
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
            # Фиксируем успех в Infrastructure Memory для всех аккаунтов из params
            try:
                from services.infra_memory import record_account_op

                for _acc_id in params.get("account_ids") or []:
                    record_account_op(
                        int(_acc_id), op_type, success=True, duration_s=duration_seconds
                    )
            except Exception:
                pass

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
            except Exception:
                pass
            # Попытаться поставить на повтор перед тем как помечать как failed
            requeued = await _maybe_requeue(pool, op_id, e, params, op_type)
            if not requeued:
                await pool.execute(
                    "UPDATE operation_queue SET status='failed', finished_at=now(), error_msg=$1 WHERE id=$2",
                    str(e)[:500],
                    op_id,
                )
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
                retry_row = await pool.fetchrow(
                    "SELECT retry_count, max_retries FROM operation_queue WHERE id=$1", op_id
                )
                retry_info = ""
                if retry_row:
                    rc = retry_row["retry_count"] or 0
                    mr = retry_row["max_retries"] or 3
                    if rc > 0:
                        retry_info = f"\nПопыток: {rc}/{mr} — лимит исчерпан"
                    else:
                        retry_info = f"\nОшибка не повторяется (фатальная)"
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

    bots_rows = await pool.fetch(
        "SELECT id, token FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
        owner_id,
    )

    ok_count = 0
    fail_count = 0

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
            await pool.execute(
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
    campaign = await pool.fetchrow(
        "SELECT id, name, status, owner_id FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
        campaign_id,
        owner_id,
    )
    if not campaign:
        return {
            "status": "failed",
            "summary": f"⚠️ DM Campaign #{campaign_id} not found or wrong owner",
        }

    if await _is_cancelled(pool, op_id):
        # Cancelled before starting — mark campaign paused so user can resume later
        await pool.execute(
            "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
        )
        return {"status": "cancelled", "summary": "Operation cancelled before start"}

    from services.dm_engine import run_campaign

    try:
        await run_campaign(pool, bot, campaign_id)
    except asyncio.CancelledError:
        # op_worker cancelled this asyncio task — mark campaign paused
        try:
            await pool.execute(
                "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
            )
        except Exception:
            pass
        raise

    # Read final counts from dm_campaigns for the completion summary
    final = await pool.fetchrow(
        "SELECT status, sent_count, fail_count, total_targets FROM dm_campaigns WHERE id=$1",
        campaign_id,
    )
    if final:
        sent = final["sent_count"] or 0
        failed = final["fail_count"] or 0
        total = final["total_targets"] or 0
        name = campaign["name"] or f"#{campaign_id}"
        summary = f"📨 DM «{name}»: ✅ {sent} sent, ❌ {failed} errors, 📊 {total} total"
        return {
            "status": "done",
            "ok": sent,
            "failed": failed,
            "total": total,
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
    )
    if not accounts_raw:
        return {"status": "failed", "summary": "⚠️ Нет активных аккаунтов"}

    accounts_rows = await _claim_available_accounts(op_id, accounts_raw)
    mp_used_acc_ids = [int(a["id"]) for a in accounts_rows]

    if not accounts_rows:
        return {
            "status": "failed",
            "summary": "⚠️ Mass Publish: все аккаунты заняты другой операцией",
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

    acc_ids = [a["id"] for a in accounts_rows]
    chan_filter = (
        "AND mc.channel_id = ANY($3::bigint[])" if explicit_channel_ids else ""
    )
    fetch_params: list = [owner_id, acc_ids]
    if explicit_channel_ids:
        fetch_params.append(explicit_channel_ids)
    db_pairs = await pool.fetch(
        f"SELECT "
        f"mc.channel_id AS id, mc.title, mc.access_hash, mc.type, "
        f"a.id AS acc_id, a.session_str, a.first_name, a.phone, "
        f"a.device_model, a.system_version, a.app_version, "
        f"a.lang_code, a.system_lang_code, a.proxy_id, p.proxy_url, p.geo_country "
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
                    "access_hash": row["access_hash"] or 0,
                    "type": row["type"] or "channel",
                },
                "accounts": [],
            }
        target_map[channel_id]["accounts"].append(acc)

    targets = list(target_map.values())
    total = len(targets)
    await pool.execute(
        "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
    )

    ok_count = 0
    fail_count = 0
    failed_channels: list[str] = []
    isolated_accounts: set[int] = set()

    for idx, target_entry in enumerate(targets, 1):
        dialog = target_entry["dialog"]
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
            remaining = total - idx + 1
            fail_count += remaining
            ch_label = str(dialog.get("title") or dialog["id"])[:60]
            if ch_label not in failed_channels:
                failed_channels.append(ch_label)
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'error',$4)",
                op_id,
                idx,
                str(dialog["id"]),
                "Аккаунт временно изолирован после сетевого/прокси сбоя",
            )
            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+$2 WHERE id=$1",
                op_id,
                remaining,
            )
            break
        flood_wait = 0
        _published = False
        last_error = ""
        for _attempt in range(2):  # per-item retry: 1 initial + 1 retry on FloodWait
            try:
                result = await account_manager.post_to_channel(
                    acc["session_str"],
                    dialog["id"],
                    mp_text,
                    access_hash=dialog["access_hash"],
                    _acc=acc,
                    media_file_id=media_file_id,
                    media_type=media_type,
                )
                if result.get("proxy_error"):
                    raise ConnectionError(
                        str(result.get("error", "proxy/network error"))
                    )
                if "error" in result or result.get("banned"):
                    raise Exception(str(result.get("error", "publish error")))
                ok_count += 1
                _published = True
                _infra_mem.record_account_op(acc["id"], "publish", success=True)
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
                    log_exc_swallow(log, "mass_publish: record_success failed")
                break  # success — stop retry loop
            except Exception as e:
                err_str = str(e)[:200]
                last_error = err_str
                flood_wait = extract_flood_wait(e, err_str)
                if _is_network_or_proxy_error(err_str):
                    isolated_accounts.add(acc["id"])
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
                                dialog["id"],
                                mp_text,
                                access_hash=dialog["access_hash"],
                                _acc=fallback_acc,
                                media_file_id=media_file_id,
                                media_type=media_type,
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
                            if _is_network_or_proxy_error(err_str):
                                isolated_accounts.add(fallback_acc["id"])
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
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'error',$4)",
                op_id,
                idx,
                str(dialog["id"]),
                err_str,
            )

        await pool.execute(
            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
        )
        if delay > 0 and idx < total:
            effective_delay = max(delay, float(flood_wait) + 5) if flood_wait else delay
            await asyncio.sleep(effective_delay)

    await release_accounts(mp_used_acc_ids)
    parts = [f"Опубликовано: {ok_count}", f"ошибок: {fail_count}"]
    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "failed_channels": failed_channels[:50],
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
    )

    accounts = await _claim_available_accounts(op_id, accounts)
    used_acc_ids = [int(a["id"]) for a in accounts]

    if not accounts:
        return {
            "status": "failed",
            "summary": "⚠️ Bulk Join: все аккаунты заняты другой операцией",
        }

    try:
        return await _exec_bulk_join_inner(pool, bot, op_id, owner_id, params, accounts)
    finally:
        await release_accounts(used_acc_ids)


async def _exec_bulk_join_inner(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict, accounts
) -> dict:
    from services import account_manager, session_simulator
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

    for acc_idx, acc in enumerate(accounts):
        acc_dict = dict(acc)
        try:
            joins_today = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_audit "
                "WHERE account_id=$1 AND action='join' AND result='success' "
                "AND occurred_at > NOW() - INTERVAL '24 hours'",
                acc["id"],
            )
        except Exception:
            joins_today = 0
        if (joins_today or 0) >= day_limit:
            log.info(
                "bulk_join: аккаунт %s достиг дневного лимита join (%d), пропуск",
                acc_dict.get("phone"),
                day_limit,
            )
            skipped_by_limit += 1
            continue
        for i, link in enumerate(links):
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
                if flood_wait:
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
                    "flood_wait" if flood_wait else "error",
                    operation_id=op_id,
                    account_id=acc["id"],
                    target=link,
                    error_msg=err_str,
                    flood_wait_s=flood_wait or None,
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
            # Apply pacing based on delay_mode from params
            chaos = session_simulator.chaos_factor()
            tod = session_simulator.time_of_day_factor()
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


async def _exec_bulk_leave(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выйти из списка каналов/групп несколькими аккаунтами."""
    from services import account_manager, session_simulator
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
    )
    accounts = await _claim_available_accounts(op_id, accounts_raw)
    used_acc_ids = [int(a["id"]) for a in accounts]

    if not accounts:
        return {
            "status": "failed",
            "summary": "⚠️ Bulk Leave: все аккаунты заняты другой операцией",
        }

    ok_count = 0
    fail_count = 0
    step = 0
    skipped_by_limit = 0
    failed_channels: list[str] = []
    delay_mode = params.get("delay_mode", "smart")
    _LEAVE_DAY_LIMITS = {"fast": 25, "normal": 20, "slow": 10, "smart": 15}
    day_limit = _LEAVE_DAY_LIMITS.get(delay_mode, 15)

    for acc_idx, acc in enumerate(accounts):
        acc_dict = dict(acc)
        try:
            leaves_today = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_audit "
                "WHERE account_id=$1 AND action='leave' AND result='success' "
                "AND occurred_at > NOW() - INTERVAL '24 hours'",
                acc["id"],
            )
        except Exception:
            leaves_today = 0
        if (leaves_today or 0) >= day_limit:
            log.info(
                "bulk_leave: аккаунт %s достиг дневного лимита leave (%d), пропуск",
                acc_dict.get("phone"),
                day_limit,
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
                left = await account_manager.leave_channel(
                    acc["session_str"], channel, _acc=acc_dict
                )
                if not left:
                    raise Exception(f"leave_channel returned False for {channel}")
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
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id,
                    step,
                    str(channel),
                    err_str,
                )
                await pool.execute(
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
            tod = session_simulator.time_of_day_factor()
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


async def _exec_global_presence_channel(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать каналы или группы для всех ожидающих целей плана global_presence."""
    from services import account_manager, session_simulator
    import random

    plan_id = params.get("plan_id")
    if not plan_id:
        return {"status": "failed", "reason": "Не указан plan_id"}

    plan = await pool.fetchrow(
        "SELECT asset_type FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )
    if not plan:
        return {"status": "failed", "reason": "План не найден"}

    asset_type = plan.get("asset_type", "channel")
    is_group = asset_type == "group"

    await pool.execute(
        "UPDATE global_presence_plans SET status='running', updated_at=now() WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )

    targets = await pool.fetch(
        "SELECT * FROM global_presence_targets WHERE plan_id=$1 AND status='pending' ORDER BY id",
        plan_id,
    )
    if not targets:
        await pool.execute(
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

    accounts_rows = await resource_selector.select_all_active(
        pool, owner_id, include_ids=acc_ids, respect_cooldown=False
    )
    acc_by_id = {a["id"]: dict(a) for a in accounts_rows}

    created_count = 0
    failed_count = 0
    total = len(targets)
    _gp_eco_id: int | None = None  # lazily loaded from plan

    for i, target in enumerate(targets):
        if await _is_cancelled(pool, op_id):
            await pool.execute(
                "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1",
                plan_id,
            )
            return {
                "status": "cancelled",
                "created": created_count,
                "failed": failed_count,
                "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
            }

        acc_id = target["selected_account_id"]
        acc = acc_by_id.get(acc_id)

        if not acc:
            await pool.execute(
                "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                "Аккаунт недоступен",
                target["id"],
            )
            failed_count += 1
            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            continue

        # ── Проверка trust_score аккаунта перед использованием ──
        trust_score = acc.get("trust_score") or 0.5
        if trust_score < 0.3:
            log.warning(
                "op_worker gp_%s: skipping account %s with low trust_score=%.2f",
                "group" if is_group else "channel",
                acc["phone"],
                trust_score,
            )
            # Попробовать найти альтернативный аккаунт с лучшим trust_score
            alt_acc = None
            for a in accounts_rows:
                if a["id"] != acc_id and (a.get("trust_score") or 0.5) >= 0.5:
                    alt_acc = dict(a)
                    log.info(
                        "op_worker gp: switching to account %s with trust=%.2f",
                        a["phone"],
                        a.get("trust_score"),
                    )
                    break

            if not alt_acc:
                await pool.execute(
                    "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                    f"Все аккаунты имеют низкий trust_score (мин: {trust_score:.2f})",
                    target["id"],
                )
                failed_count += 1
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1",
                    op_id,
                )
                continue

            acc = alt_acc

        # Atomic claim: only proceed if target is still 'pending' to prevent duplicate processing
        claimed = await pool.execute(
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

        # Генерируем описание: пустое описание = немедленный spam-сигнал для Telegram
        _geo_label = (target.get("city") or target.get("country") or "").strip()
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
            await pool.execute(
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
            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            await asyncio.sleep(
                random.uniform(10, 25) * session_simulator.chaos_factor()
            )
            continue

        channel_id = result.get("channel_id")
        channel_access_hash = result.get("access_hash", 0)

        username_error = None
        planned_username = target.get("planned_username")
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

        # ── Атомарная запись: обновить targets + вставить в managed_channels одной транзакцией.
        # Если Telethon создал канал, но DB-запись падает, канал станет «призраком» без записи.
        # Транзакция гарантирует: либо оба write успешны, либо оба откатываются.
        async with pool.acquire() as _conn:
            async with _conn.transaction():
                await _conn.execute(
                    "UPDATE global_presence_targets SET status='done', result_asset_id=$1 WHERE id=$2",
                    channel_id,
                    target["id"],
                )
                await _conn.execute(
                    """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username)
                       VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4""",
                    owner_id,
                    acc["id"],
                    channel_id,
                    title,
                    target.get("planned_username") or None,
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
        except Exception:
            pass

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
        except Exception:
            pass

        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
            op_id,
            created_count + failed_count,
            f"{target.get('city', '?')} → {title}",
            f"channel_id={channel_id}"
            + (f" | username_err={username_error}" if username_error else ""),
        )
        await pool.execute(
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
                await asyncio.sleep(cooldown)
            else:
                # Короткая пауза между операциями
                delay = random.uniform(45, 90) * chaos * tod_factor * jitter
                await asyncio.sleep(delay)

    final_status = (
        "done" if failed_count == 0 else ("failed" if created_count == 0 else "done")
    )
    await pool.execute(
        "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
        final_status,
        plan_id,
    )

    return {
        "status": "done",
        "created": created_count,
        "failed": failed_count,
        "plan_id": plan_id,
        "summary": f"Создано каналов: {created_count}, ошибок: {failed_count}",
    }


async def _exec_global_presence_bot(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать ботов через BotFather для каждой цели плана global_presence."""
    from services import account_manager, session_simulator
    import random

    plan_id = params.get("plan_id")
    if not plan_id:
        return {"status": "failed", "reason": "no plan_id in params"}

    plan = await pool.fetchrow(
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
        except Exception:
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
        await pool.execute(
            "UPDATE global_presence_plans SET status='failed', updated_at=now() WHERE id=$1",
            plan_id,
        )
        return {"status": "failed", "reason": "no active accounts found"}

    # Build lookup by id for per-target account assignment (mirrors _exec_global_presence_channel)
    acc_by_id = {a["id"]: dict(a) for a in accounts_rows}
    # Fallback list for round-robin when target has no selected_account_id
    accounts_list = list(accounts_rows)

    targets = await pool.fetch(
        "SELECT * FROM global_presence_targets WHERE plan_id=$1 AND status='pending' ORDER BY id",
        plan_id,
    )
    if not targets:
        await pool.execute(
            "UPDATE global_presence_plans SET status='done', updated_at=now() WHERE id=$1",
            plan_id,
        )
        return {"status": "done", "created": 0, "failed": 0, "plan_id": plan_id}

    await pool.execute(
        "UPDATE global_presence_plans SET status='running', updated_at=now() WHERE id=$1",
        plan_id,
    )

    created_count = 0
    failed_count = 0
    acc_rr_idx = 0  # round-robin index for fallback only
    total = len(targets)
    _gp_bot_eco_id: int | None = None  # lazily loaded from plan

    for i, target in enumerate(targets):
        if await _is_cancelled(pool, op_id):
            await pool.execute(
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
        claimed = await pool.execute(
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
            await pool.execute(
                "UPDATE global_presence_targets SET status='pending' WHERE id=$1",
                target["id"],
            )
            await asyncio.sleep(wait_s)
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
            await pool.execute(
                "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                str(result["error"])[:500],
                target["id"],
            )
            failed_count += 1
            _infra_mem.record_account_op(
                acc["id"],
                "global_presence_bot",
                success=False,
                error=str(result["error"])[:100],
            )
            await _audit(
                pool,
                owner_id,
                "gp_create_bot",
                "error",
                operation_id=op_id,
                account_id=acc["id"],
                target=bot_name[:100],
                error_msg=str(result["error"])[:200],
            )
            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            await asyncio.sleep(random.uniform(30, 60))
            continue

        token = result.get("token", "")
        actual_username = result.get("username", bot_username)

        # Save bot to managed_bots (token format: "{bot_id}:{hash}")
        try:
            from database import db as _db

            if token and ":" in token:
                bot_id_int = int(token.split(":")[0])
                await _db.add_bot(
                    pool, token, bot_id_int, actual_username, bot_name, owner_id
                )
        except Exception as e:
            log.warning("op_worker gp_bot: managed_bots insert failed: %s", e)

        await pool.execute(
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
        except Exception:
            pass

        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
            op_id,
            created_count + failed_count + 1,
            f"{target.get('city', '?')} → @{actual_username}",
            f"bot created: @{actual_username}",
        )
        await pool.execute(
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

        # Humanized delay between BotFather interactions
        await asyncio.sleep(random.uniform(60, 120) * session_simulator.chaos_factor())

    final_status = (
        "done" if failed_count == 0 else ("failed" if created_count == 0 else "done")
    )
    await pool.execute(
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


_MIN_ACCOUNT_AGE_DAYS = 14  # минимальный возраст аккаунта в системе для bulk-операций
_MIN_TRUST_SCORE = 0.35  # минимальный trust_score для создания каналов
_MAX_CHANNELS_PER_DAY = 2  # максимум каналов в сутки с одного аккаунта


async def _exec_bulk_create_channels(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Массовое создание каналов через Telethon с умными задержками (из AI-ассистента)."""
    from services import account_manager, session_simulator
    import random
    from datetime import datetime, timezone

    prefix = params.get("prefix", "Channel")
    count = int(params.get("count", 5))
    about = params.get("about", "")
    username_pattern = params.get("username_pattern", "")
    acc_id = params.get("acc_id", 0)

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

    # ── Account health gate ───────────────────────────────────────────────────
    acc_data = await pool.fetchrow(
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
    created_today = await pool.fetchval(
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
        title = f"{prefix} #{num}"
        if username_pattern:
            username = f"{username_pattern}_{num}"
        else:
            username = ""

        # Human-like typing delay
        await session_simulator.typing_delay(title)

        result = await account_manager.create_channel(
            acc["session_str"], title, about=about, _acc=acc
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
                    acc["session_str"], title, about=about, _acc=acc
                )

        if (
            isinstance(result, dict)
            and result.get("channel_id")
            and not result.get("error")
        ):
            ch_id = result["channel_id"]
            # Save to managed_channels
            await pool.execute(
                """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4""",
                owner_id,
                acc["id"],
                ch_id,
                title,
                username or None,
            )
            # Set username if pattern provided — 60-120s delay prevents geo-ban detection
            if username:
                await asyncio.sleep(random.uniform(60, 120))
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

            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                op_id,
                num,
                f"{title}",
                f"channel_id={ch_id}" + (f" @{username}" if username else ""),
            )
            created_count += 1
        else:
            err_msg = result if isinstance(result, str) else str(result)
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                op_id,
                num,
                f"{title}",
                err_msg[:200],
            )
            failed_count += 1

        await pool.execute(
            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
        )

        if i < count - 1:
            tod_factor = session_simulator.time_of_day_factor()
            chaos = session_simulator.chaos_factor()
            if i % 5 == 4:
                cooldown = random.uniform(300, 600) * chaos * tod_factor
                log.info(
                    "op_worker bulk_channels: cooldown %.0fs after %d items",
                    cooldown,
                    i + 1,
                )
                await asyncio.sleep(cooldown)
            else:
                delay = random.uniform(45, 90) * chaos * tod_factor
                await asyncio.sleep(delay)

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

    return {
        "status": "done",
        "created": created_count,
        "failed": failed_count,
        "summary": f"Создано каналов: {created_count}, ошибок: {failed_count}",
    }


async def _exec_bot_factory(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать ботов через @BotFather FSM с умными задержками.

    Params:
      acc_id         — int, id аккаунта в tg_accounts
      count          — int, количество ботов (1-10)
      name_template  — str, шаблон имени: "My Bot" → "My Bot 1", "My Bot 2"...
      uname_template — str, шаблон username: "mybot" → "mybot1_bot", "mybot2_bot"...
    """
    from services import account_manager, session_simulator
    import random

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

    created_count = 0
    failed_count = 0
    created_tokens: list[str] = []

    for i in range(count):
        if await _is_cancelled(pool, op_id):
            return {
                "status": "cancelled",
                "ok": created_count,
                "failed": failed_count,
                "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
            }

        num = i + 1
        display_name = f"{name_tpl} {num}" if count > 1 else name_tpl
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
                log_exc_swallow(log, f"_exec_bot_factory: getMe failed for token {token[:20]}...")

            # Save to managed_bots
            try:
                await pool.execute(
                    """INSERT INTO managed_bots(added_by, token, bot_id, username, first_name, is_active)
                       VALUES($1,$2,$3,$4,$5,TRUE)
                       ON CONFLICT(bot_id) DO UPDATE SET token=$2, username=$4, is_active=TRUE""",
                    owner_id,
                    token,
                    bot_id or 0,
                    actual_uname,
                    display_name,
                )
            except Exception:
                log_exc_swallow(log, f"_exec_bot_factory: managed_bots upsert failed")

            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'ok',$4)",
                op_id,
                num,
                display_name,
                f"@{actual_uname} token={token[:20]}...",
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
                            await pool.execute(
                                """INSERT INTO managed_bots(added_by, token, bot_id, username, first_name, is_active)
                                   VALUES($1,$2,0,$3,$4,TRUE)
                                   ON CONFLICT(token) DO UPDATE SET username=$3, is_active=TRUE""",
                                owner_id, token, actual_uname, display_name,
                            )
                        except Exception:
                            pass
                        created_count += 1
                        await pool.execute(
                            "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
                            op_id, num, display_name, f"@{actual_uname} (retry ok)",
                        )
                        await pool.execute(
                            "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                        )
                        await asyncio.sleep(random.uniform(30, 60))
                        continue

            failed_count += 1
            await pool.execute(
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

        await pool.execute(
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

    # ── Загрузить аккаунты через resource_selector (flood-aware + cooldown) ───
    raw_accounts = await resource_selector.select_all_active(
        pool,
        owner_id,
        include_ids=account_ids or None,
        respect_cooldown=False,  # preflight_accounts делает свою cooldown проверку
        action_type="strike",
    )

    if not raw_accounts:
        return {"status": "failed", "summary": "⚠️ Strike: нет аккаунтов"}

    accounts_dicts = [dict(a) for a in raw_accounts]

    # ── Pre-flight: фильтр cooldown + flood-state + сортировка ────────────────
    viable = preflight_accounts(accounts_dicts)
    if not viable:
        return {
            "status": "failed",
            "summary": "⚠️ Strike: все аккаунты в cooldown или неактивны",
        }

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

    if not viable:
        return {
            "status": "failed",
            "summary": "⚠️ Strike: все аккаунты на прогреве или в cooldown",
        }

    # ── Волны ─────────────────────────────────────────────────────────────────
    waves = plan_waves(viable, num_waves=num_waves)

    await pool.execute(
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
        except Exception:
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
            except Exception:
                pass

    try:
        results = await staggered_strike(plan, progress_cb=_strike_progress, pool=pool)
    except Exception as e:
        log.exception("op_worker _exec_strike #%d failed: %s", op_id, e)
        return {
            "status": "failed",
            "summary": f"❌ Strike завершился с ошибкой: {str(e)[:200]}",
        }

    await pool.execute(
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
                       spambot_escalation)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
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
    import aiohttp as _aiohttp
    from collections import defaultdict
    from services import broadcaster

    text: str = str(params.get("text") or "").strip()
    segment: str = str(params.get("segment") or "all_each")
    lang: str = str(params.get("lang") or "")
    selected_bot_ids: list[int] = [int(x) for x in (params.get("selected_bot_ids") or [])]
    cluster_name: str = str(params.get("cluster_name") or "")

    if not text:
        return {"status": "failed", "summary": "⚠️ Текст рассылки не указан"}

    bots_all = await db.get_bots(pool, owner_id)
    if not bots_all:
        return {"status": "failed", "summary": "⚠️ Нет ботов для рассылки"}

    # Apply segment filter to bot list
    if segment == "selected_bots" and selected_bot_ids:
        bots = [b for b in bots_all if b["bot_id"] in set(selected_bot_ids)]
    elif segment == "cluster" and cluster_name:
        cluster_bot_rows = await pool.fetch(
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
    _BOT_START_DELAY_S = 2.0

    async with _aiohttp.ClientSession() as http:
        if segment in ("all_each", "selected_bots", "cluster"):
            for b in bots:
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
                bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id)
                if not bc_id:
                    continue
                broadcaster.start(
                    pool, http, bc_id, b["token"], b["bot_id"], text, None, ids, None,
                    start_delay=total_started * _BOT_START_DELAY_S,
                )
                total_started += 1
                total_users += len(ids)
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+$1 WHERE id=$2",
                    len(ids), op_id,
                )

        elif segment == "unique":
            users = await db.get_unique_network_users(pool, owner_id)
            by_bot: dict = defaultdict(list)
            token_map: dict = {}
            for u in users:
                by_bot[u["bot_id"]].append(u["user_id"])
                token_map[u["bot_id"]] = u["token"]
            for bid, ids in by_bot.items():
                bc_id = await db.create_broadcast(pool, bid, text, len(ids), owner_id)
                if not bc_id:
                    continue
                broadcaster.start(
                    pool, http, bc_id, token_map[bid], bid, text, None, ids, None,
                    start_delay=total_started * _BOT_START_DELAY_S,
                )
                total_started += 1
                total_users += len(ids)
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+$1 WHERE id=$2",
                    len(ids), op_id,
                )

        elif segment in ("cold_all", "lost_all"):
            days_from = 30 if segment == "lost_all" else 7
            days_to = None if segment == "lost_all" else 30
            for b in bots:
                ids = await db.get_inactive_user_ids(pool, b["bot_id"], days_from, days_to)
                if not ids:
                    continue
                bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id)
                if not bc_id:
                    continue
                broadcaster.start(
                    pool, http, bc_id, b["token"], b["bot_id"], text, None, ids, None,
                    start_delay=total_started * _BOT_START_DELAY_S,
                )
                total_started += 1
                total_users += len(ids)
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+$1 WHERE id=$2",
                    len(ids), op_id,
                )

        elif segment == "lang":
            for b in bots:
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
                bc_id = await db.create_broadcast(pool, b["bot_id"], text, len(ids), owner_id)
                if not bc_id:
                    continue
                broadcaster.start(
                    pool, http, bc_id, b["token"], b["bot_id"], text, None, ids, None,
                    start_delay=total_started * _BOT_START_DELAY_S,
                )
                total_started += 1
                total_users += len(ids)
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+$1 WHERE id=$2",
                    len(ids), op_id,
                )

    if total_started == 0:
        return {
            "status": "done",
            "ok": 0,
            "summary": "⚠️ Нет пользователей в выбранном сегменте — рассылка не запущена",
        }

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
        except Exception:
            return []

    ch_ids: list[int] = _jlist(pack["channel_ids"])
    if not ch_ids:
        return {"status": "done", "summary": "⚠️ В пакете нет каналов — посев пропущен"}

    # Resolve bot token if bot is linked
    bot_token: str | None = None
    if pack.get("bot_id"):
        try:
            bot_row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                pack["bot_id"], owner_id,
            )
            if bot_row:
                bot_token = bot_row["token"]
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

    success = 0
    fail = 0
    fail_names: list[str] = []
    total = len(channels)

    async with _aiohttp.ClientSession() as http:
        for idx, ch in enumerate(channels, 1):
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
            posted = False
            if bot_token:
                chan_target = (
                    f"@{ch['username']}"
                    if ch.get("username")
                    else int(f"-100{ch['channel_id']}")
                )
                posted = await _ps.seed_channel_post(http, bot_token, chan_target, post_text)
            if not posted:
                posted = await _ps.seed_channel_via_account(
                    pool, owner_id, ch["channel_id"], ch.get("access_hash") or 0, post_text
                )
            if posted:
                success += 1
            else:
                fail += 1
                fail_names.append(chan_name)

            # Update progress in operation_queue
            try:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                    idx, op_id,
                )
            except Exception:
                pass

            await asyncio.sleep(2)

    if success > 0:
        try:
            await db.mark_presence_pack_seeded(pool, pack_id, owner_id)
        except Exception:
            log.warning(
                "_exec_seed_presence_pack op=%d: mark_presence_pack_seeded failed", op_id
            )

    fail_hint = ""
    if fail_names:
        names = ", ".join(fail_names[:3])
        extra = f" (+{len(fail_names) - 3})" if len(fail_names) > 3 else ""
        fail_hint = f"\n⚠️ Не удалось: {names}{extra}"

    return {
        "status": "done",
        "ok": success,
        "fail": fail,
        "total": total,
        "summary": (
            f"🌱 Посев постов Presence Pack #{pack_id}\n"
            f"✅ Опубликовано: {success}/{total}{fail_hint}"
        ),
    }
