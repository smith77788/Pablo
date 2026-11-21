"""Очередь недоставленных уведомлений: отчёт владельцу доходит, даже если
Telegram не принял его с первых попыток.

ПОЧЕМУ. `db.notify_if_enabled` пробует отправить три раза за ~5 секунд. Сбой
сети, перегрузка Telegram или его лимит длиннее этого окна означали, что
уведомление пропадало НАВСЕГДА: анти-повтор отметку снимал, но повторять было
некому — итог операции событие однократное, второго повода сообщить о нём не
будет. Симптом из прода (21.11.2026): «отчёт об операции не доехал до
владельца — 21 раз(а)».

КАК. Временный сбой доставки кладёт сообщение сюда (`enqueue`), и вызывающий
считает его доставленным — ответственность за него теперь у очереди. Фоновый
цикл лидера (`run`) раз в `_TICK_S` берёт созревшие строки и шлёт их с
нарастающей паузой (`_BACKOFF_S`) до `_MAX_AGE_S`. Строки берутся под
`FOR UPDATE SKIP LOCKED` с одновременным сдвигом срока, поэтому два процесса
одну строку не возьмут, а упавший процесс отдаст свою через срок попытки.

Окончательная потеря (сутки повторов или сбой самой записи в очередь) — это
`infragram_notifications_undelivered_total`, то есть тревога владельцу
приходит, только когда отчёт действительно не дошёл, а не на каждый блип.

Цена: если процесс умрёт между ответом Telegram и удалением строки, отчёт
придёт дважды. Это осознанно — второй отчёт дешевле потерянного, а реального
действия здесь нет.
"""
from __future__ import annotations

import asyncio
import logging

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_TICK_S = 30.0
_BATCH = 20
# Пауза перед попыткой номер N (после первой неудачи в notify_if_enabled).
_BACKOFF_S = (30, 60, 120, 300, 600, 1200, 1800, 3600)
# Дольше суток повторять незачем: отчёт устарел, а очередь не должна расти.
_MAX_AGE_S = 24 * 3600
# Строк на одного получателя. Больше — это уже не блип, а получатель, который
# недоступен долго; копить дальше значит завалить его, когда он вернётся.
_PER_USER_CAP = 200
# Сколько держим взятую строку, пока идёт отправка. Упавший процесс отдаёт её
# соседу через этот срок.
_CLAIM_S = 300

_DELAYED_NOTE = "⏳ <i>Отчёт задержался: Telegram не принял его вовремя.</i>\n\n"


def _metric(name: str, **labels) -> None:
    try:
        from services import metrics as _m

        _m.inc(name, labels or None)
    except Exception:
        pass


def backoff_for(attempts: int) -> int:
    """Пауза перед следующей попыткой. ЧИСТАЯ."""
    i = max(0, int(attempts) - 1)
    return _BACKOFF_S[min(i, len(_BACKOFF_S) - 1)]


def _markup_to_text(markup) -> "str | None":
    if markup is None:
        return None
    try:
        return markup.model_dump_json(exclude_none=True)
    except Exception:
        return None


def _markup_from_text(raw: "str | None"):
    if not raw:
        return None
    try:
        from aiogram.types import InlineKeyboardMarkup

        return InlineKeyboardMarkup.model_validate_json(raw)
    except Exception:
        return None


async def enqueue(pool, user_id: int, pref: str, text: str, reply_markup=None,
                  *, error: str = "", delay_s: float = 0.0) -> bool:
    """Положить недоставленное уведомление в очередь. True — легло.

    Никогда не бросает: зовётся на пути обработки сбоя доставки. False значит,
    что очередь его не взяла (база недоступна, или получателю и так копится
    слишком много), и вызывающий обязан считать уведомление потерянным.
    """
    if pool is None or not text:
        return False
    try:
        row_id = await pool.fetchval(
            """INSERT INTO notification_outbox
                   (user_id, pref, text, reply_markup, last_error, next_attempt_at)
               SELECT $1, $2, $3, $4, $5, now() + make_interval(secs => $6)
                WHERE (SELECT COUNT(*) FROM notification_outbox
                        WHERE user_id = $1) < $7
               RETURNING id""",
            int(user_id), str(pref or "")[:40], str(text),
            _markup_to_text(reply_markup), str(error or "")[:300],
            float(max(delay_s, backoff_for(1))), _PER_USER_CAP)
    except Exception:
        log_exc_swallow(log, f"notify_outbox.enqueue user={user_id}")
        return False
    # BIGSERIAL начинается с 1: пусто или 0 — строка не легла.
    if not row_id:
        return False
    _metric("infragram_notifications_deferred_total", pref=str(pref or "")[:40])
    return True


async def _claim(pool) -> list:
    return await pool.fetch(
        """UPDATE notification_outbox o
              SET attempts = o.attempts + 1,
                  next_attempt_at = now() + make_interval(secs => $2)
            WHERE o.id IN (
                  SELECT id FROM notification_outbox
                   WHERE next_attempt_at <= now()
                   ORDER BY id
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED)
           RETURNING o.id, o.user_id, o.pref, o.text, o.reply_markup,
                     o.attempts,
                     EXTRACT(EPOCH FROM (now() - o.created_at)) AS age_s""",
        _BATCH, float(_CLAIM_S))


async def _send(bot, user_id: int, text: str, markup) -> None:
    """Отправить; ошибка разметки/длины — простым текстом, как notify_if_enabled."""
    from database import db

    body = _DELAYED_NOTE + text
    try:
        await bot.send_message(user_id, body, parse_mode="HTML",
                               reply_markup=markup)
        return
    except Exception as exc:
        if not db._notify_failure_is_content_error(exc):
            raise
    plain = db._html_to_plain(body)
    if len(plain) > db._TG_MESSAGE_LIMIT:
        plain = plain[:db._TG_MESSAGE_LIMIT - 1].rstrip() + "…"
    await bot.send_message(user_id, plain, reply_markup=markup)


async def deliver_once(pool, bot) -> int:
    """Один проход по созревшим строкам. Возвращает число доставленных."""
    from database import db

    if bot is None:
        return 0
    try:
        rows = await _claim(pool)
    except Exception:
        log_exc_swallow(log, "notify_outbox: очередь не прочиталась")
        return 0
    delivered = 0
    for r in rows:
        rid, uid, pref = int(r["id"]), int(r["user_id"]), r["pref"] or ""
        try:
            await _send(bot, uid, r["text"], _markup_from_text(r["reply_markup"]))
        except Exception as exc:
            permanent = (db._notify_failure_is_permanent(exc)
                         or db._notify_failure_is_content_error(exc))
            expired = float(r["age_s"] or 0) >= _MAX_AGE_S
            if permanent or expired:
                await _drop(pool, rid)
                if db._notify_failure_is_permanent(exc):
                    _metric("infragram_notifications_recipient_gone_total",
                            pref=pref[:40])
                else:
                    # Окончательная потеря: вот о ней владелец и должен узнать.
                    _metric("infragram_notifications_undelivered_total",
                            pref=pref[:40])
                    log.error(
                        "notify_outbox: НЕ ДОСТАВЛЕНО user=%s pref=%s после %s "
                        "попыток: %s", uid, pref, r["attempts"], str(exc)[:200])
                continue
            wait = backoff_for(int(r["attempts"]) + 1)
            retry_after = getattr(exc, "retry_after", None)
            try:
                if retry_after:
                    wait = max(wait, int(float(retry_after)) + 1)
            except (TypeError, ValueError):
                pass
            try:
                await pool.execute(
                    "UPDATE notification_outbox "
                    "SET next_attempt_at = now() + make_interval(secs => $2), "
                    "    last_error = $3 WHERE id = $1",
                    rid, float(wait), str(exc)[:300])
            except Exception:
                # Срок захвата (_CLAIM_S) уже стоит — строка вернётся сама.
                log_exc_swallow(log, f"notify_outbox: срок повтора {rid} не записан")
            continue
        delivered += 1
        await _drop(pool, rid)
    if delivered:
        _metric("infragram_notifications_redelivered_total")
    return delivered


async def _drop(pool, rid: int) -> None:
    try:
        await pool.execute("DELETE FROM notification_outbox WHERE id = $1", rid)
    except Exception:
        # Не удалилась — через _CLAIM_S уйдёт повтором: лишний отчёт дешевле
        # потерянного.
        log_exc_swallow(log, f"notify_outbox: строка {rid} не удалена")


async def run(pool, bot) -> None:
    """Фоновый цикл доставки. Сбой прохода не роняет цикл."""
    log.info("notify_outbox: доставка отложенных уведомлений (раз в %.0f с)", _TICK_S)
    while True:
        try:
            await deliver_once(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc_swallow(log, "notify_outbox.run")
        await asyncio.sleep(_TICK_S)
