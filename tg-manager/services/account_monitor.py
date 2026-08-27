"""Background service: monitor active account count per owner, alert on low count.

Also: ping Telegram to verify session liveness, mark dead sessions as
session_expired, and notify account owners.
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncpg
from aiogram import Bot
from database import db
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_INTERVAL = 3600  # check every hour
_MIN_ACCOUNTS = 2  # alert threshold
_LOW_TRUST_THRESHOLD = 0.3  # trust_score below this triggers alert
_STALE_RUNNING_HOURS = 3  # running ops older than this are considered stuck
_ALERT_COOLDOWN = 86400  # 24h between repeated low-account alerts per owner
_SESSION_EXPIRED_COOLDOWN = 86400  # 24h между алертами «сессия истекла» на аккаунт


async def _check_and_alert(pool: asyncpg.Pool, bot: Bot) -> None:
    """Find owners with fewer than _MIN_ACCOUNTS active accounts and notify them."""
    rows = await pool.fetch(
        """
        SELECT ta.owner_id, COUNT(*) AS active_count
        FROM tg_accounts ta
        WHERE ta.is_active = true
        GROUP BY ta.owner_id
        HAVING COUNT(*) < $1
        """,
        _MIN_ACCOUNTS,
    )
    if not rows:
        return

    for row in rows:
        owner_id = row["owner_id"]
        active_count = row["active_count"]
        # Персистентный дедуп (переживает рестарт): не чаще раза в 24ч на владельца.
        if not await db.notify_dedup_ok(
            pool, owner_id, "low_accounts", _ALERT_COOLDOWN
        ):
            continue
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "flood_warning",
            f"⚠️ <b>Внимание: мало активных аккаунтов</b>\n\n"
            f"У вас осталось активных TG-аккаунтов: <b>{active_count}</b>\n"
            f"Рекомендуется иметь минимум {_MIN_ACCOUNTS} активных аккаунта "
            f"для корректной работы сервиса.\n\n"
            f"Добавьте аккаунты в разделе <b>Аккаунты</b>.",
        )
        log.info(
            "account_monitor: alerted owner=%s (active=%s)", owner_id, active_count
        )


async def _check_low_trust(pool: asyncpg.Pool, bot: Bot) -> None:
    """Alert owners when accounts drop to critically low trust_score."""
    try:
        rows = await pool.fetch(
            """
            SELECT ta.owner_id, ta.phone, ta.first_name, ta.username,
                   ta.trust_score, ta.flood_count_7d
            FROM tg_accounts ta
            WHERE ta.is_active = true
              AND ta.trust_score < $1
              AND (ta.last_low_trust_alert IS NULL
                   OR ta.last_low_trust_alert < NOW() - INTERVAL '6 hours')
            """,
            _LOW_TRUST_THRESHOLD,
        )
    except Exception as e:
        log.debug("low_trust_check: %s (column may not exist yet)", e)
        return

    by_owner: dict[int, list] = {}
    for r in rows:
        by_owner.setdefault(r["owner_id"], []).append(r)

    for owner_id, accs in by_owner.items():
        names = [r["username"] or r["first_name"] or r["phone"] or "id?" for r in accs]
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "restriction",
            f"🔴 <b>Критически низкий trust_score</b>\n\n"
            f"Аккаунты: <b>{', '.join(names[:5])}</b>\n"
            f"Trust score ниже {_LOW_TRUST_THRESHOLD:.1f} — высокий риск бана.\n\n"
            f"Рекомендации:\n"
            f"• Не запускайте операции через эти аккаунты 48ч\n"
            f"• Откройте Health Dashboard → 💡 Рекомендации\n"
            f"• Проверьте аккаунты вручную в Telegram",
        )
        # Mark as alerted (best-effort)
        try:
            await pool.execute(
                "UPDATE tg_accounts SET last_low_trust_alert=NOW() "
                "WHERE owner_id=$1 AND trust_score < $2 AND is_active=true",
                owner_id,
                _LOW_TRUST_THRESHOLD,
            )
        except Exception:
            log_exc_swallow(
                log, "сбой алерта low trust — не удалось обновить last_low_trust_alert"
            )
        log.info(
            "account_monitor: low trust alert for owner=%s (%d accounts)",
            owner_id,
            len(accs),
        )


# Персистентный дедуп (БД) переживает РЕСТАРТ бота — раньше in-memory кулдаун
# сбрасывался при каждом перезапуске, и один и тот же алерт «риск бана» слался
# заново каждые несколько минут (жалоба на назойливые уведомления). 24ч на
# аккаунт: совет в самом алерте — пауза 24–48ч, поэтому чаще раза в сутки незачем.
_BAN_RISK_ALERT_COOLDOWN = 86400  # 24h между алертами по одному аккаунту


async def _check_ban_risk(pool: asyncpg.Pool, bot: Bot) -> None:
    """Раннее предупреждение о риске бана по ПОВЕДЕНИЮ (operation_audit за 24ч).

    Дополняет _check_low_trust (статический trust_score) поведенческим сигналом:
    всплеск flood/ban/провалов за сутки обычно предшествует падению trust.
    Уведомляет только на 'critical' (risk_score≥70), дедуп 6ч на аккаунт.
    Кандидаты ограничены теми, у кого реально были тревожные события — дёшево."""
    from services import behavioral_engine

    try:
        cand = await pool.fetch(
            """SELECT DISTINCT a.id, a.owner_id, a.username, a.first_name, a.phone
               FROM tg_accounts a
               JOIN operation_audit oa ON oa.account_id = a.id
               WHERE a.is_active = true
                 AND oa.occurred_at > now() - INTERVAL '24 hours'
                 AND (oa.result IN ('flood_wait','banned','error','failed')
                      OR oa.error_msg ILIKE '%flood%')
               LIMIT 25"""
        )
    except Exception as e:
        log.debug("ban_risk_check: candidate query failed: %s", e)
        return

    for acc in cand:
        try:
            pred = await behavioral_engine.predict_ban_risk(pool, acc["id"])
        except Exception:
            log_exc_swallow(log, "ban_risk_check: predict failed")
            continue
        if pred.get("risk_level") != "critical":
            continue
        # Персистентный дедуп (переживает рестарт): не чаще раза в 24ч на аккаунт.
        if not await db.notify_dedup_ok(
            pool, acc["owner_id"], f"ban_risk:{acc['id']}", _BAN_RISK_ALERT_COOLDOWN
        ):
            continue
        label = acc["username"] or acc["first_name"] or acc["phone"] or str(acc["id"])
        reasons = "; ".join(pred.get("reasons", [])[:3]) or "аномальная активность"
        await db.notify_if_enabled(
            pool,
            bot,
            acc["owner_id"],
            "restriction",
            f"🚨 <b>Критический риск бана</b> (поведение за 24ч)\n\n"
            f"Аккаунт: <b>{label}</b>\n"
            f"Risk score: <b>{pred.get('risk_score', 0)}/100</b>\n"
            f"Причины: {reasons}\n\n"
            f"Остановите операции через этот аккаунт на 24–48ч и проверьте его вручную.\n\n"
            f"<i>Отключить эти уведомления: Настройки → Уведомления → «⚠️ Критические "
            f"ошибки (флуд/ограничения)».</i>",
            dedup_key=f"ban_risk:{acc['id']}",
        )
        log.warning(
            "account_monitor: ban risk CRITICAL acc=%d owner=%d score=%s",
            acc["id"], acc["owner_id"], pred.get("risk_score"),
        )


async def _recover_stuck_operations(pool: asyncpg.Pool, bot: Bot) -> None:
    """Mark operations stuck in 'running' for too long as failed."""
    try:
        stuck = await pool.fetch(
            """
            SELECT id, owner_id, op_type
            FROM operation_queue
            WHERE status = 'running'
              AND started_at < NOW() - ($1 * INTERVAL '1 hour')
            """,
            _STALE_RUNNING_HOURS,
        )
        for row in stuck:
            await pool.execute(
                "UPDATE operation_queue SET status='failed', finished_at=NOW(), "
                "error_msg='Операция зависла (таймаут 3ч) — перезапустите вручную' "
                "WHERE id=$1 AND status='running'",
                row["id"],
            )
            await db.notify_if_enabled(
                pool,
                bot,
                row["owner_id"],
                "op_complete",
                f"⚠️ <b>Операция #{row['id']} зависла</b>\n\n"
                f"Тип: {row['op_type']}\n"
                f"Операция выполнялась более {_STALE_RUNNING_HOURS}ч без завершения.\n"
                f"Статус изменён на failed. Перезапустите из раздела Operations → Отчёты.",
            )
            log.warning("account_monitor: marked stuck op id=%d as failed", row["id"])
    except Exception as exc:
        log.debug("account_monitor: stuck ops check error: %s", exc)


async def _heal_expired_cooldowns(pool: asyncpg.Pool) -> None:
    """Пассивный само-heal: снять acc_status='cooldown' → 'active', когда
    cooldown_until истёк.

    op_worker при сетевом/прокси-сбое ставит acc_status='cooldown' +
    cooldown_until (обычно +15 мин), но НИЧТО не возвращало статус в 'active'
    после истечения окна: reactivate-запрос в check_accounts_health бьёт только
    по is_active=FALSE, а здесь аккаунт остаётся включённым. В итоге один
    FloodWait 15 минут назад держал аккаунт в «⚠️ Под риском» бесконечно (пульс
    считает 'cooldown' риском), пока пользователь не запустит проверку вручную.

    Организм должен заживать сам: как только окно кулдауна прошло — статус
    чистый. Только 'cooldown' (транзиентный статус от op_worker); 'warming'/
    'banned'/'session_expired' не трогаем.
    """
    try:
        res = await pool.execute(
            """UPDATE tg_accounts
               SET acc_status='active', status_reason=NULL
               WHERE acc_status='cooldown'
                 AND is_active=TRUE
                 AND (cooldown_until IS NULL OR cooldown_until <= NOW())
                 -- Кулдаун за УСТОЙЧИВЫЙ конфликт сессии не снимаем: иначе
                 -- самолечение и проверка здоровья начнут перебрасывать статус
                 -- туда-сюда, и оператор так и не увидит, что сессию надо
                 -- перезалить. Отметку снимет первая же чистая проверка.
                 AND session_conflict_at IS NULL""",
        )
        n = int(str(res).rsplit(" ", 1)[-1]) if str(res).rsplit(" ", 1)[-1].isdigit() else 0
        if n:
            log.info("account_monitor: self-heal — снят истёкший кулдаун с %d аккаунтов", n)
    except Exception as exc:
        log.debug("account_monitor: heal_expired_cooldowns error: %s", exc)


async def _check_dead_sessions(pool: asyncpg.Pool, bot: Bot) -> None:
    """Ping Telegram for accounts whose session hasn't been verified recently.

    Checks up to 5 accounts per cycle (those not checked in the last 3 hours).
    On auth failure: sets acc_status='session_expired', is_active=FALSE,
    and notifies the owner.  Uses should_persist_account_status() to avoid
    flip-flopping on transient errors.
    """
    from services.account_manager import check_account_status_full, should_persist_account_status

    try:
        # Поля транспорта берём из ОБЩЕГО списка (db.telethon_accounts_query):
        # свой SELECT здесь не содержал cf_relay_url и proxy_url, поэтому
        # проверка шла НАПРЯМУЮ с host-IP, пока операции того же аккаунта идут
        # через релей. Одна сессия с двух адресов — AUTH_KEY_DUPLICATED, то есть
        # проверка здоровья сама портила аккаунты, которые проверяет.
        from database.db import telethon_accounts_query as _tq

        accounts = await pool.fetch(
            _tq(
                """a.is_active = TRUE
                   AND a.session_str IS NOT NULL AND a.session_str != ''
                   -- НЕ трогаем аккаунты, занятые операцией: параллельный коннект
                   -- одной сессии монитором и операцией = AUTH_KEY_DUPLICATED.
                   -- Особенно важно для СВЕЖЕГО флота (last_real_check_at IS NULL
                   -- → монитор берёт его первым, ровно когда оператор запускает op).
                   AND COALESCE(a.in_operation, FALSE) = FALSE
                   AND (a.last_real_check_at IS NULL
                        OR a.last_real_check_at < NOW() - INTERVAL '3 hours')"""
            )
            + " ORDER BY COALESCE(a.last_real_check_at, '2000-01-01') ASC LIMIT 5",
        )
    except Exception as exc:
        log.debug("_check_dead_sessions: query error: %s", exc)
        return

    if not accounts:
        return

    log.info("account_monitor: dead-session check — %d accounts", len(accounts))

    for acc in accounts:
        # Атомарный захват у арбитра op_worker ПЕРЕД коннектом: check_account_
        # status_full открывает ЖИВУЮ сессию. Снимок is_account_in_use оставлял
        # окно гонки — операция захватывала аккаунт между проверкой и коннектом →
        # одна сессия с двух IP = AUTH_KEY_DUPLICATED. Особенно критично для
        # СВЕЖЕГО флота: монитор берёт его первым (last_real_check_at IS NULL)
        # ровно в момент старта операции. Освобождаем сразу после проверки —
        # дальше только БД-обновления, сессия уже закрыта.
        _opw = None
        _leased = True
        try:
            from services import op_worker as _opw_mod
            _opw = _opw_mod
            _leased = await _opw.try_claim_account(int(acc["id"]))
        except Exception:
            _opw = None
            _leased = True
        if not _leased:
            continue
        try:
            result = await asyncio.wait_for(
                check_account_status_full(
                    acc["session_str"], dict(acc), check_spambot=False
                ),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            log.debug("account_monitor: session ping timeout acc=%d", acc["id"])
            continue
        except Exception as exc:
            log_exc_swallow(log, "account_monitor dead-session check acc=%d: %s", acc["id"], exc)
            continue
        finally:
            if _opw and _leased:
                try:
                    await _opw.release_accounts([int(acc["id"])])
                except Exception:
                    log_exc_swallow(log, "account_monitor: release acc=%d", acc["id"])

        status = result.get("status", "active")
        auth_error = result.get("auth_error", False)

        # Always update the check timestamp
        try:
            await pool.execute(
                "UPDATE tg_accounts SET last_real_check_at=now() WHERE id=$1",
                acc["id"],
            )
        except Exception:
            log_exc_swallow(log, "account_monitor: failed to update last_real_check_at acc=%d", acc["id"])

        if result.get("no_session"):
            continue

        if not should_persist_account_status(
            status, auth_error=auth_error, has_session=True
        ):
            continue

        # Session is confirmed dead
        if status in ("session_expired", "banned", "deactivated") and auth_error:
            try:
                # Через account_status.set_status, а не прямым UPDATE: триггер БД
                # поймает смену в любом случае, но человекочитаемую ПРИЧИНУ в
                # событие пишет только этот путь. Без неё разбор потерь
                # показывает смерть без объяснения, откуда она взялась.
                from services import account_status as _acc_status

                await _acc_status.set_status(
                    pool,
                    acc["id"],
                    status,
                    reason=str(auth_error)[:200],
                    source="account_monitor",
                )
                await pool.execute(
                    "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1",
                    acc["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, "account_monitor: failed to update dead acc=%d", acc["id"]
                )

            # Уведомление владельцу — персистентный дедуп (переживает рестарт),
            # раз в 24ч на аккаунт. Раньше in-memory словарь сбрасывался при
            # перезапуске и слал «сессия истекла» заново.
            if await db.notify_dedup_ok(
                pool, acc["owner_id"], f"sess_expired:{acc['id']}",
                _SESSION_EXPIRED_COOLDOWN,
            ):
                label = acc.get("username") or acc.get("first_name") or acc.get("phone") or str(acc["id"])
                await db.notify_if_enabled(
                    pool,
                    bot,
                    acc["owner_id"],
                    "restriction",
                    f"🔴 <b>Сессия аккаунта истекла</b>\n\n"
                    f"Аккаунт: <b>@{label}</b>\n"
                    f"Статус: <b>{status}</b>\n\n"
                    "Telegram отклонил авторизацию — сессия мертва.\n"
                    "Аккаунт деактивирован автоматически.\n\n"
                    "Переимпортируйте сессию в разделе <b>Аккаунты</b>.",
                    dedup_key=f"sess_expired:{acc['id']}",
                )
                log.warning(
                    "account_monitor: dead session acc=%d owner=%d status=%s",
                    acc["id"],
                    acc["owner_id"],
                    status,
                )

        await asyncio.sleep(2)  # small pause between account checks


async def check_owner_now(pool: asyncpg.Pool, bot: Bot, owner_id: int) -> None:
    """Immediate check for a specific owner — call after deactivating an account."""
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
        owner_id,
    )
    cnt = row["cnt"] if row else 0
    if cnt < _MIN_ACCOUNTS:
        await db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "restriction",
            f"⚠️ <b>Аккаунт деактивирован</b>\n\n"
            f"Один из ваших TG-аккаунтов был автоматически деактивирован "
            f"(получен PeerFlood или бан).\n"
            f"Активных аккаунтов осталось: <b>{cnt}</b>.\n\n"
            f"Добавьте новые аккаунты в разделе <b>Аккаунты</b>.",
        )


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background loop: check account health every hour."""
    await asyncio.sleep(300)  # startup delay: 5 min
    cycle = 0
    while True:
        try:
            await _check_and_alert(pool, bot)
            await _check_low_trust(pool, bot)
            await _check_ban_risk(pool, bot)
            # Само-heal: снять истёкшие кулдауны (дёшево, каждый цикл) — иначе
            # один давний FloodWait держит аккаунт в «Под риском» навсегда.
            await _heal_expired_cooldowns(pool)
            # Check for stuck operations every 3 cycles (every 3 hours)
            if cycle % 3 == 0:
                await _recover_stuck_operations(pool, bot)
            # Ping Telegram sessions every cycle to detect dead sessions
            await _check_dead_sessions(pool, bot)
            cycle += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("account_monitor error: %s", exc)
        await asyncio.sleep(_INTERVAL)
