"""Auto-Funnel — automated message sequences delivered to bot audience segments."""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import asyncpg
from aiogram import Bot

from services import bot_api

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 60   # seconds between processing cycles
_BATCH_SIZE    = 50   # max runs to process per cycle


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    # TLS-верификация включена (по умолчанию в aiohttp). Раньше здесь снималась
    # проверка сертификата на всех исходящих вызовах воронки (webhook'и/внешние
    # API), что открывало MITM. Внешние эндпоинты доверенные — проверка обязательна.
    http = aiohttp.ClientSession()

    log.info("Auto-Funnel service started")
    try:
        while True:
            try:
                await _process_due_runs(pool, http)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Auto-Funnel loop error: %s", e)
            await asyncio.sleep(_LOOP_INTERVAL)
    finally:
        await http.close()


async def _process_due_runs(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    runs = await pool.fetch(
        """
        SELECT r.*, f.bot_id AS f_bot_id
        FROM auto_funnel_runs r
        JOIN auto_funnels f ON f.id = r.funnel_id
        WHERE r.status = 'active'
          AND r.next_send_at <= NOW()
        ORDER BY r.next_send_at
        LIMIT $1
        """,
        _BATCH_SIZE,
    )
    for run in runs:
        try:
            await _process_run(pool, http, run)
        except Exception as e:
            # БЛИП — НЕ КОНЕЦ ВОРОНКИ. Любое исключение писало здесь
            # `status='error'`, а само исключение уходило в `log.debug`:
            # воронка подписчику заканчивалась молча и насовсегда. Постоянные
            # причины `_process_run` разбирает сам (нет шага → completed, нет
            # токена → error), поэтому наружу вылетает ровно транзиентное —
            # блип БД, таймаут Telegram, занятый пул. Повтор дёшев и
            # безопасен: шаг уже занят (`last_sent_step`), так что второй
            # отправки он не сделает.
            await _note_run_failure(pool, run, e)


_RUN_FAIL_LIMIT = 5
_RUN_FAIL_BACKOFF_MIN = 15


async def _note_run_failure(pool: asyncpg.Pool, run, exc: Exception) -> None:
    """Срыв шага: отодвинуть запуск, а бросить воронку — только по исчерпании.

    Счётчик подряд идущих срывов живёт в строке (`fail_count`), потому что
    решение обязано переживать рестарт: процесс перезапускается на каждом
    деплое, и счёт в памяти означал бы бесконечные повторы.
    """
    try:
        left = await pool.fetchval(
            """UPDATE auto_funnel_runs
                  SET fail_count = COALESCE(fail_count, 0) + 1,
                      next_send_at = NOW() + make_interval(mins => $2),
                      status = CASE WHEN COALESCE(fail_count, 0) + 1 >= $3
                                    THEN 'error' ELSE status END
                WHERE id = $1
            RETURNING COALESCE(fail_count, 0)""",
            run["id"], _RUN_FAIL_BACKOFF_MIN, _RUN_FAIL_LIMIT,
        )
    except Exception as write_exc:
        log.error(
            "Auto-Funnel: срыв шага run=%s (%s) НЕ ЗАПИСАН (%s) — счёт попыток "
            "не двинулся, запуск придёт на следующий круг как есть",
            run["id"], exc, write_exc)
        return
    if left is not None and int(left) >= _RUN_FAIL_LIMIT:
        log.error(
            "Auto-Funnel: воронка остановлена для run=%s — %d срывов подряд, "
            "последний: %s", run["id"], int(left), exc)
    else:
        log.warning(
            "Auto-Funnel: шаг run=%s сорвался (%s), попытка %s из %d — "
            "переносим на %d мин, воронку не бросаем",
            run["id"], exc, left, _RUN_FAIL_LIMIT, _RUN_FAIL_BACKOFF_MIN)


async def _process_run(pool: asyncpg.Pool, http: aiohttp.ClientSession, run) -> None:
    funnel_id = run["funnel_id"]
    step_num  = run["next_step_num"]

    step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 AND step_num=$2",
        funnel_id, step_num,
    )
    if not step:
        # No more steps — mark completed
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='completed' WHERE id=$1", run["id"]
        )
        return

    # Get bot token
    from database.db import fetchrow_bot as _fetchrow_bot_af
    bot_row = await _fetchrow_bot_af(
        pool, "SELECT token FROM managed_bots WHERE bot_id=$1", run["bot_id"]
    )
    if not bot_row:
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='error' WHERE id=$1", run["id"]
        )
        return

    # Build optional button
    buttons = None
    if step["button_text"] and step["button_url"]:
        buttons = [{"text": step["button_text"], "url": step["button_url"]}]

    # ОТМЕТКА ДО ОТПРАВКИ. Отправка шла первой, а продвижение
    # (`next_step_num`/`next_send_at`) писалось после неё. Между этими двумя
    # действиями процесс умирает на каждом деплое рабочей ветки: строка
    # остаётся с тем же шагом и `next_send_at <= NOW()`, и следующий круг
    # опроса посылает тот же шаг тому же ЖИВОМУ человеку заново. Условный
    # UPDATE занимает шаг атомарно; не прошёл — значит шаг уже уходил, и этот
    # заход отправку пропускает и просто двигает воронку дальше. Потерянное
    # сообщение дешевле второго: второе человек читает как спам, а бот
    # получает за него блокировку.
    _claimed = await pool.fetchval(
        """UPDATE auto_funnel_runs SET last_sent_step=$2
            WHERE id=$1 AND COALESCE(last_sent_step, 0) < $2
        RETURNING id""",
        run["id"], int(step_num),
    )
    if _claimed is None:
        log.info(
            "Auto-Funnel: шаг %d воронки %s уже отправлялся (run=%s) — "
            "пропускаем отправку, двигаем дальше",
            step_num, funnel_id, run["id"])
    else:
        ok, _retry_after, _cat = False, 0, "network"
        try:
            ok, _retry_after, _cat = await bot_api.send_message_classified(
                http,
                bot_row["token"],
                run["user_id"],
                step["message_text"],
                buttons=buttons,
            )
        except Exception as e:
            log.debug("Auto-Funnel: send error run %d: %s", run["id"], e)

        if not ok:
            # РАЗНИЦА МЕЖДУ «ЗАБЛОКИРОВАЛ» И «НЕ ДОШЛО». Любая неудача
            # означала здесь «User likely blocked the bot» и глушила воронку
            # этому человеку НАВСЕГДА — то есть сетевой блип или 5xx Telegram
            # выписывали живого подписчика из воронки. Решает та же дверь, что
            # у рассылки (`bot_api.recipient_is_gone`): ушёл — останавливаем,
            # не дошло — шаг потерян, но воронка идёт дальше.
            if bot_api.recipient_is_gone(_cat):
                await pool.execute(
                    "UPDATE auto_funnel_runs SET status='stopped' WHERE id=$1",
                    run["id"],
                )
                return
            log.info(
                "Auto-Funnel: шаг %d не доставлен (%s) run=%s — воронку не "
                "обрываем, идём к следующему шагу",
                step_num, _cat, run["id"])

    # Find next step
    next_step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 AND step_num > $2 ORDER BY step_num LIMIT 1",
        funnel_id, step_num,
    )
    if not next_step:
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='completed' WHERE id=$1", run["id"]
        )
        return

    # Schedule next step
    from datetime import timedelta
    next_at = datetime.now(timezone.utc) + timedelta(hours=next_step["delay_hours"])
    # fail_count=0: счётчик считает срывы ПОДРЯД. Без обнуления воронка,
    # которая раз в месяц спотыкается о блип, через пять месяцев получила бы
    # 'error' на ровном месте.
    await pool.execute(
        "UPDATE auto_funnel_runs SET next_step_num=$1, next_send_at=$2, "
        "fail_count=0 WHERE id=$3",
        next_step["step_num"], next_at, run["id"],
    )


async def launch_funnel(
    pool: asyncpg.Pool,
    funnel_id: int,
    owner_id: int,
    segment: str,
) -> int:
    """Enqueue runs for all matching users in the segment. Returns count of new runs."""
    funnel = await pool.fetchrow(
        "SELECT * FROM auto_funnels WHERE id=$1 AND owner_id=$2",
        funnel_id, owner_id,
    )
    if not funnel:
        return 0

    # Get first step to know initial delay
    first_step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num LIMIT 1",
        funnel_id,
    )
    if not first_step:
        return 0

    from datetime import timedelta

    first_send_at = datetime.now(timezone.utc) + timedelta(hours=first_step["delay_hours"])

    # Segment query
    if segment == "all":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND suspect=FALSE",
            funnel["bot_id"],
        )
    elif segment == "new_7d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND suspect=FALSE AND first_seen >= NOW() - INTERVAL '7 days'",
            funnel["bot_id"],
        )
    elif segment == "new_30d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND suspect=FALSE AND first_seen >= NOW() - INTERVAL '30 days'",
            funnel["bot_id"],
        )
    elif segment == "inactive_30d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND suspect=FALSE AND last_seen < NOW() - INTERVAL '30 days'",
            funnel["bot_id"],
        )
    else:
        return 0

    if not users:
        return 0

    # Bulk upsert runs (ignore existing to avoid re-sending)
    count = 0
    for u in users:
        result = await pool.execute(
            """
            INSERT INTO auto_funnel_runs (funnel_id, bot_id, user_id, next_step_num, next_send_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (funnel_id, user_id) DO NOTHING
            """,
            funnel_id, funnel["bot_id"], u["user_id"],
            first_step["step_num"], first_send_at,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count
