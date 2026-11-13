"""Background scheduler: fires due scheduled broadcasts every 60 seconds.

Checks for pending scheduled broadcasts and triggers them when their
scheduled time arrives. Also handles A/B experiment winner selection.

Usage:
    from services.scheduler import run

    # Starts the background scheduler loop
    await run(pool, http)
"""

from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import aiohttp
import asyncpg
from database import db
from services import broadcaster
from services import bot_api

log = logging.getLogger(__name__)

# Рассылки опаздывающие больше чем на 1 час → помечаем как 'missed', не запускаем.
# Переопределяется через переменную окружения SCHEDULER_MISSED_THRESHOLD_HOURS.
_MISSED_THRESHOLD = timedelta(
    hours=float(os.environ.get("SCHEDULER_MISSED_THRESHOLD_HOURS", "1"))
)

# Минимальный возраст активного эксперимента до попытки объявить победителя.
# Предотвращает досрочное завершение при малой выборке сразу после старта.
_AB_MIN_AGE = timedelta(hours=float(os.environ.get("AB_WINNER_MIN_AGE_HOURS", "24")))


# Занятие строки расписания (status='processing') живёт секунды: проверка
# токена, создание записи рассылки, запуск задачи. Строка, провисевшая в нём
# дольше порога, заведомо брошена умершим процессом, а не выполняется.
_STUCK_CLAIM_MIN = float(os.environ.get("SCHEDULER_STUCK_CLAIM_MIN", "15"))


async def _ensure_claim_columns(pool: asyncpg.Pool) -> None:
    """Досоздать колонки восстановления, если миграция v219 ещё не доехала.

    Деплой и миграции расходятся во времени; без колонок сторож падал бы на
    каждом тике, то есть восстановление исчезало бы целиком вместо того, чтобы
    подождать. Тот же приём, что у op_worker._ensure_revive_column.
    """
    try:
        await pool.execute(
            "ALTER TABLE scheduled_broadcasts "
            "ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ, "
            "ADD COLUMN IF NOT EXISTS broadcast_id BIGINT"
        )
    except Exception:
        log.warning("Scheduler: ensure claim columns failed", exc_info=True)


async def scheduled_bot_state(pool, creator_id: int, bot_id: int, token: str | None = None,
                              *, lock: bool = False) -> str:
    """Статус допуска для создателя расписания и текущего токена."""
    from services.mini_app_api import _BOTS_VISIBLE_SQL
    from services.token_vault import decrypt_token

    bot = await pool.fetchrow(
        f"""SELECT mb.is_active, mb.token FROM managed_bots mb
            WHERE mb.bot_id=$2 AND ({_BOTS_VISIBLE_SQL})"""
        + (" FOR SHARE OF mb" if lock else ""),
        creator_id, bot_id,
    )
    if not bot:
        return "cancelled"
    if not bot["is_active"] or (token is not None and decrypt_token(bot["token"]) != token):
        return "pending"
    return "ready"


async def _reschedule(pool, row) -> int | None:
    if not row["repeat_interval_min"]:
        return None
    async with pool.acquire() as conn, conn.transaction():
        # Архивирование сначала меняет бота. При том же порядке блокировок
        # следующее вхождение либо успеет записаться, либо уже не появится.
        state = await scheduled_bot_state(conn, row["created_by"], row["bot_id"], lock=True)
        if state == "cancelled":
            return None
        return await db.reschedule_if_recurring(conn, dict(row))


async def recover_stuck_claims(pool: asyncpg.Pool) -> dict[str, int]:
    """Вернуть к жизни расписания, брошенные умершим процессом.

    Планировщик занимает строку переводом в 'processing' и снимает занятость
    только сам. Процесс умер между занятием и завершением — а на Railway это
    каждый деплой — и строка остаётся в 'processing' НАВСЕГДА:
    get_pending_scheduled выбирает только 'pending'. Отложенная рассылка молча
    не уходит и молча не отчитывается, а у повторяемого расписания вместе с ней
    обрывается вся дальнейшая цепочка.

    Вслепую возвращать в 'pending' нельзя: если процесс умер ПОСЛЕ
    create_broadcast, второй запуск создал бы ВТОРУЮ рассылку на всю аудиторию.
    Поэтому решает broadcast_id:

      • есть → рассылка создана, её докатит broadcaster.resume_interrupted
        (он пропускает уже доставленных по журналу, без дублей); расписание
        закрываем как выполненное и продлеваем, если оно повторяемое;
      • нет → спрашиваем саму рассылку по обратной ссылке
        `broadcasts.scheduled_id` (schema_v298). `broadcast_id` пишется
        ОТДЕЛЬНЫМ запросом после создания рассылки, и в зазоре между ними
        пусто значит не «работы не было», а «рассылка создана, но мы об этом
        не записали»: она уйдёт сама, а расписание завело бы вторую на ту же
        аудиторию. Нашлась — закрываем и дописываем связь;
      • ни того, ни другого → работа правда не начиналась, честно возвращаем
        в очередь, а дальше обычная логика решит: запускать или пометить
        'missed'.

    Возвращает {"closed": N, "requeued": M}. Никогда не бросает: это уборка, а
    не критический путь.
    """
    stats = {"closed": 0, "requeued": 0}
    try:
        rows = await pool.fetch(
            # message_text и created_by нужны reschedule_if_recurring: без них
            # продление повторяемого расписания падало бы на KeyError, и цепочка
            # обрывалась бы ровно там, где мы её чиним.
            """SELECT id, bot_id, message_text, created_by, execute_at,
                      repeat_interval_min, broadcast_id
                 FROM scheduled_broadcasts
                WHERE status = 'processing'
                  AND (claimed_at IS NULL
                       OR claimed_at < NOW() - make_interval(mins => $1))""",
            _STUCK_CLAIM_MIN,
        )
    except Exception:
        log.warning("Scheduler: не удалось найти брошенные занятия", exc_info=True)
        return stats

    for row in rows or []:
        try:
            _bc = row["broadcast_id"]
            if not _bc:
                # Вторая запись могла не состояться (процесс умер в зазоре,
                # база не приняла UPDATE), а рассылка при этом уже создана и
                # уйдёт сама. Поэтому спрашиваем саму рассылку по обратной
                # ссылке (schema_v298), прежде чем сказать «работа не
                # начиналась». СБОЙ ЭТОГО ЧТЕНИЯ ЗАКРЫТ: пустой ответ означал
                # бы ровно то, чего мы боимся, — второй залп на всю аудиторию.
                try:
                    _bc = await pool.fetchval(
                        "SELECT id FROM broadcasts WHERE scheduled_id = $1 "
                        "ORDER BY id LIMIT 1",
                        row["id"],
                    )
                except Exception:
                    log.warning(
                        "Scheduler: не прочитать рассылку расписания #%d — "
                        "занятие оставлено как есть, иначе рассылка могла бы "
                        "уйти второй раз", row["id"], exc_info=True)
                    continue
                if _bc:
                    log.warning(
                        "Scheduler: у расписания #%d рассылка #%s создана, но "
                        "связь не записалась — закрываем по обратной ссылке",
                        row["id"], _bc)
                    await pool.execute(
                        "UPDATE scheduled_broadcasts SET broadcast_id=$2 "
                        "WHERE id=$1 AND broadcast_id IS NULL",
                        row["id"], int(_bc),
                    )
            if _bc:
                changed = await pool.execute(
                    "UPDATE scheduled_broadcasts SET status='done' "
                    "WHERE id=$1 AND status='processing'",
                    row["id"],
                )
                if changed == "UPDATE 0":
                    continue
                stats["closed"] += 1
                # Повторяемое расписание не должно оборваться на рестарте.
                try:
                    await _reschedule(pool, row)
                except Exception:
                    log.exception(
                        "Scheduler: продление после восстановления #%d не удалось",
                        row["id"])
            else:
                changed = await pool.execute(
                    "UPDATE scheduled_broadcasts SET status='pending', claimed_at=NULL "
                    "WHERE id=$1 AND status='processing' AND broadcast_id IS NULL",
                    row["id"],
                )
                if changed != "UPDATE 0":
                    stats["requeued"] += 1
        except Exception:
            log.exception("Scheduler: восстановление расписания #%d не удалось", row["id"])

    if stats["closed"] or stats["requeued"]:
        log.warning(
            "Scheduler: восстановлено брошенных занятий — закрыто %d (рассылка уже "
            "создана), возвращено в очередь %d",
            stats["closed"], stats["requeued"],
        )
    return stats


async def _token_state(http: aiohttp.ClientSession, token: str) -> str:
    """См. `bot_api.token_state` — одно правило для всех проверок токена."""
    return await bot_api.token_state(http, token)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    # Track scheduled IDs currently being processed to prevent duplicate firing
    # within a single scheduler cycle (in case processing takes longer than sleep interval).
    _in_flight: set[int] = set()
    _ab_sweep_cycle = 0  # run AB winner sweep every 60 cycles (≈1 hour)
    _recover_cycle = 0

    await _ensure_claim_columns(pool)
    # На старте — сразу: мы и есть тот процесс, который поднялся после рестарта,
    # оборвавшего занятия. Ждать пятнадцать циклов тут нечего.
    await recover_stuck_claims(pool)

    while True:
        try:
            rows = await db.get_pending_scheduled(pool)
            now = datetime.now(timezone.utc)
            for row in rows:
                # Skip if already being processed in this scheduler instance
                if row["id"] in _in_flight:
                    continue
                admission = await scheduled_bot_state(pool, row["created_by"], row["bot_id"], row["token"])
                if admission != "ready":
                    if admission == "cancelled":
                        await pool.execute(
                            "UPDATE scheduled_broadcasts SET status='cancelled' "
                            "WHERE id=$1 AND status='pending'", row["id"],
                        )
                    continue
                execute_at = row["execute_at"]
                # Нормализуем timezone если нужно
                if execute_at is not None and execute_at.tzinfo is None:
                    execute_at = execute_at.replace(tzinfo=timezone.utc)

                # Если рассылка опоздала больше чем на 1 час — помечаем как 'missed'
                if execute_at is not None and (now - execute_at) > _MISSED_THRESHOLD:
                    try:
                        res = await pool.execute(
                            "UPDATE scheduled_broadcasts SET status='missed' "
                            "WHERE id=$1 AND status='pending'",
                            row["id"],
                        )
                        if res == "UPDATE 0":
                            # Владелец отменил расписание после выборки: не
                            # затирать 'cancelled' и не ставить следующее.
                            continue
                        # Повторяемое: пропуск одной итерации не должен рвать цепочку.
                        try:
                            await _reschedule(pool, row)
                        except Exception:
                            log.exception("Scheduler: reschedule(missed) failed for #%d", row["id"])
                        log.warning(
                            "Scheduler: scheduled #%d missed (execute_at=%s, now=%s) — marking missed",
                            row["id"],
                            execute_at,
                            now,
                        )
                    except Exception:
                        log.exception(
                            "Scheduler: failed to mark scheduled #%d as missed",
                            row["id"],
                        )
                    continue

                # Atomically claim this scheduled broadcast to prevent duplicate firing.
                # Uses status='processing' as a transient state; reverted to 'pending'
                # on failure if no broadcast was created yet.
                claimed = await pool.execute(
                    # claimed_at обязателен: по нему сторож отличает живое
                    # занятие от брошенного умершим процессом.
                    "UPDATE scheduled_broadcasts "
                    "SET status='processing', claimed_at=NOW() "
                    "WHERE id=$1 AND status='pending'",
                    row["id"],
                )
                if claimed == "UPDATE 0":
                    # Already claimed by another scheduler instance or concurrent cycle
                    continue
                _in_flight.add(row["id"])

                bc_id = None
                created_bc = False
                try:
                    # Pre-flight: verify bot token before creating broadcast records
                    token_state = await _token_state(http, row["token"])
                    if token_state == "transient":
                        # Telegram недоступен (сеть, 5xx, флуд) — это не
                        # «токен отозван». Раньше любой неуспех getMe навсегда
                        # помечал расписание 'failed', и у повторяемого вместе с
                        # ним обрывалась вся цепочка. Возвращаем в очередь:
                        # следующий цикл попробует снова, а затянувшийся сбой
                        # закроет порог «пропущено», который цепочку сохраняет.
                        log.warning(
                            "Scheduler: Telegram недоступен для расписания #%d "
                            "(bot_id=%d) — повторю на следующем цикле",
                            row["id"], row["bot_id"])
                        await pool.execute(
                            "UPDATE scheduled_broadcasts SET status='pending' "
                            "WHERE id=$1 AND status='processing'",
                            row["id"],
                        )
                        _in_flight.discard(row["id"])
                        continue
                    total = (await db.get_audience_count(pool, row["bot_id"])
                             if token_state != "revoked" else 0)
                    async with pool.acquire() as conn, conn.transaction():
                        admission = await scheduled_bot_state(
                            conn, row["created_by"], row["bot_id"], row["token"], lock=True,
                        )
                        if admission != "ready" or token_state == "revoked":
                            status = admission if admission != "ready" else "failed"
                            await conn.execute(
                                "UPDATE scheduled_broadcasts SET status=$2, claimed_at=NULL "
                                "WHERE id=$1 AND status='processing'",
                                row["id"], status,
                            )
                        else:
                            current = await conn.fetchval(
                                "SELECT id FROM scheduled_broadcasts WHERE id=$1 AND status='processing' "
                                "AND bot_id=$2 AND created_by=$3 AND broadcast_id IS NULL FOR UPDATE",
                                row["id"], row["bot_id"], row["created_by"],
                            )
                            if current:
                                bc_id = await db.create_broadcast(
                                    conn, row["bot_id"], row["message_text"], total, row["created_by"],
                                    scheduled_id=int(row["id"]),
                                )
                                await conn.execute(
                                    "UPDATE scheduled_broadcasts SET broadcast_id=$2 WHERE id=$1",
                                    row["id"], bc_id,
                                )
                    if bc_id is None:
                        _in_flight.discard(row["id"])
                        continue
                    created_bc = True
                    broadcaster.start(
                        pool,
                        http,
                        bc_id,
                        row["token"],
                        row["bot_id"],
                        row["message_text"],
                        schedule_creator_id=row["created_by"],
                    )
                    finished = await pool.execute(
                        "UPDATE scheduled_broadcasts SET status='done' WHERE id=$1 AND status='processing'",
                        row["id"],
                    )
                    # Только победитель перехода в done продолжает цепочку.
                    try:
                        new_id = await _reschedule(pool, row) if finished != "UPDATE 0" else None
                        if new_id:
                            log.info("Scheduled #%d recurring → next #%d", row["id"], new_id)
                    except Exception:
                        log.exception("Scheduler: reschedule failed for #%d", row["id"])
                    _in_flight.discard(row["id"])
                    log.info(
                        "Scheduled #%d fired → broadcast #%d (bot %d)",
                        row["id"],
                        bc_id,
                        row["bot_id"],
                    )
                except Exception:
                    if created_bc and bc_id:
                        log.warning(
                            "Scheduler: broadcast #%d created but start failed for scheduled #%d — "
                            "marking scheduled done to prevent duplicates",
                            bc_id,
                            row["id"],
                        )
                        try:
                            await pool.execute(
                                "UPDATE scheduled_broadcasts SET status='done' WHERE id=$1 AND status='processing'",
                                row["id"],
                            )
                        except Exception:
                            log.exception(
                                "Scheduler: failed to mark scheduled #%d as done",
                                row["id"],
                            )
                    elif bc_id is None:
                        # Ошибка до получения ID: попытка создания не завершилась.
                        # Если ID получен, но commit неизвестен, сторож сверит связь.
                        try:
                            await pool.execute(
                                "UPDATE scheduled_broadcasts SET status='pending' "
                                "WHERE id=$1 AND status='processing'",
                                row["id"],
                            )
                        except Exception:
                            log.exception(
                                "Scheduler: failed to revert status for scheduled #%d",
                                row["id"],
                            )
                    _in_flight.discard(row["id"])
                    log.exception("Scheduler failed to fire scheduled #%d", row["id"])
        except Exception:
            log.exception("Scheduler loop error")

        # A/B winner sweep — once per hour.
        # Ждём НАПРЯМУЮ, а не fire-and-forget create_task: у event loop только
        # СЛАБАЯ ссылка на задачу, поэтому несохранённый create_task может быть
        # собран GC до завершения — свип победителей молча не доработал бы (а
        # именно на систему experiments мы ведём пользователя). Свип ограничен
        # (активные эксперименты) и со своим try/except; идёт перед sleep(60),
        # firing рассылок не задерживает.
        # Сторож брошенных занятий — раз в ~15 минут (цикл 60с).
        # На старте он уже отработал; здесь он ловит случай, когда процесс
        # пережил рестарт СОСЕДНЕЙ реплики.
        _recover_cycle += 1
        if _recover_cycle >= 15:
            _recover_cycle = 0
            await recover_stuck_claims(pool)

        _ab_sweep_cycle += 1
        if _ab_sweep_cycle >= 60:
            _ab_sweep_cycle = 0
            await declare_ab_winners(pool)
            # Тем же часовым тактом — самолечение денормализованного плана:
            # get_plan уже дата-aware, но platform_users.current_plan и
            # subscriptions.is_active после истечения остаются «paid»/true и
            # подтекают в админ-списки/фолбэки. Свип возвращает их в согласие.
            await expire_stale_subscriptions(pool)

        await asyncio.sleep(60)


async def expire_stale_subscriptions(pool: asyncpg.Pool) -> None:
    """Привести оба хранилища подписки в согласие для истёкших строк.

    Источник правды — subscriptions (get_plan фильтрует `expires_at > now()`),
    поэтому доступ и так снимается вовремя. Но денормализованная копия
    platform_users.current_plan и флаг subscriptions.is_active после истечения
    остаются «paid»/true навсегда — и подтекают в админ-CSV, сегментацию и
    фолбэки чтения. Идемпотентно: трогает только реально истёкшие строки, на
    свежих подписках — 0 изменений. Кеш плана здесь чистить не нужно: get_plan
    и так вернёт free по дате (кеш живёт 60с).
    """
    try:
        subs = await pool.execute(
            "UPDATE subscriptions SET is_active=false "
            "WHERE is_active=true AND expires_at <= now()"
        )
        users = await pool.execute(
            "UPDATE platform_users SET current_plan='free', plan_expires_at=NULL "
            "WHERE current_plan IS NOT NULL AND current_plan <> 'free' "
            "AND plan_expires_at IS NOT NULL AND plan_expires_at <= now()"
        )
        # Логируем только когда реально что-то истекло (иначе шум каждый час).
        if subs != "UPDATE 0" or users != "UPDATE 0":
            log.info("expire_stale_subscriptions: subs=%s users=%s", subs, users)
    except Exception:
        log.exception("expire_stale_subscriptions: sweep failed")


async def declare_ab_winners(pool: asyncpg.Pool) -> None:
    """Nightly sweep: evaluate all active A/B experiments and declare winners.

    Runs once per hour from the main scheduler loop.  Only evaluates experiments
    that have been active for at least _AB_MIN_AGE to avoid premature decisions.
    """
    try:
        cutoff = datetime.now(timezone.utc) - _AB_MIN_AGE
        active_experiments = await pool.fetch(
            """SELECT id, bot_id, name
               FROM experiments
               WHERE status = 'active'
                 AND COALESCE(started_at, created_at) <= $1""",
            cutoff,
        )
        if not active_experiments:
            return
        log.debug("AB winner sweep: checking %d experiments", len(active_experiments))
        for exp in active_experiments:
            try:
                winner_id = await db.check_experiment_winner(pool, exp["id"])
                if winner_id:
                    log.info(
                        "AB winner declared: experiment %d (bot %d) name=%r winner_variant_id=%d",
                        exp["id"],
                        exp["bot_id"],
                        exp["name"],
                        winner_id,
                    )
            except Exception:
                log.exception(
                    "declare_ab_winners: error evaluating experiment %d", exp["id"]
                )
    except Exception:
        log.exception("declare_ab_winners: sweep failed")
