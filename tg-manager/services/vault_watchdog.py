"""Сторож «Хранилища»: сам замечает, что бизнес-подключение тихо отвалилось.

Хранилище пишет историю через Telegram Business. Подключение может молча
перестать слать апдейты (истёк Premium, бота убрали, Telegram сбросил связь) —
и пользователь узнаёт об этом, только когда лезет в архив и видит старьё.

Сторож раз в несколько часов проверяет включённые подключения: если давно
(> STALE_DAYS) не приходило ни одного сообщения — ОДИН раз шлёт владельцу в ЛС
предупреждение с инструкцией переподключить. Повторно не спамит (stale_notified_at);
когда трафик возобновится — метка снимается, и при следующем зависании
предупредим снова.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

STALE_DAYS = 2                 # сколько дней тишины = «зависло»
GRACE_DAYS = 2                 # свежему подключению без трафика даём осмотреться
CHECK_EVERY = 6 * 3600         # период проверки

_TEXT = (
    "⚠️ <b>Хранилище перестало получать сообщения</b>\n\n"
    "Уже {days} дн. в ваши личные чаты не приходило ни одного нового сообщения "
    "через бизнес-бота. Обычно это значит, что подключение тихо отвалилось.\n\n"
    "<b>Частые причины:</b>\n"
    "• истёк <b>Telegram Premium</b> (Business работает только с Premium);\n"
    "• бота убрали/переустановили в настройках бизнес-аккаунта;\n"
    "• Telegram сбросил подключение.\n\n"
    "<b>Как починить:</b> Настройки Telegram → «Telegram для бизнеса» → "
    "«Чат-боты» → переподключите бота (снимите и снова добавьте), разрешите "
    "доступ к чатам. Уже сохранённый архив на месте."
)


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("vault_watchdog: starting")
    while True:
        try:
            await _check_once(pool, bot)
        except Exception:
            log.exception("vault_watchdog: check failed")
        await asyncio.sleep(CHECK_EVERY)


async def _check_once(pool: asyncpg.Pool, bot: Bot) -> int:
    """Один проход. Возвращает число отправленных предупреждений (для тестов)."""
    rows = await pool.fetch(
        """SELECT bc.connection_id, bc.owner_id, bc.user_chat_id, bc.created_at,
                  bc.stale_notified_at,
                  (SELECT MAX(msg_date) FROM vault_messages vm
                    WHERE vm.owner_id = bc.owner_id) AS last_at
           FROM business_connections bc
           WHERE bc.is_enabled = TRUE""")
    now = dt.datetime.now(dt.timezone.utc)

    def _aware(ts):
        if ts is not None and ts.tzinfo is None:
            return ts.replace(tzinfo=dt.timezone.utc)
        return ts

    sent = 0
    for r in rows:
        last_at = _aware(r["last_at"])
        created = _aware(r["created_at"])
        notified = r["stale_notified_at"]

        if last_at is not None:
            stale = (now - last_at) >= dt.timedelta(days=STALE_DAYS)
            days = max(0, int((now - last_at).total_seconds() // 86400))
        else:
            # ни одного сообщения — «зависло», только если подключение уже не
            # свежее (grace), иначе это нормальный онбординг.
            stale = created is None or (now - created) >= dt.timedelta(days=GRACE_DAYS)
            days = STALE_DAYS

        if stale and notified is None:
            chat_id = r["user_chat_id"] or r["owner_id"]
            try:
                await bot.send_message(chat_id, _TEXT.format(days=days), parse_mode="HTML")
                sent += 1
            except Exception:
                log.debug("vault_watchdog: не удалось уведомить owner=%s", r["owner_id"])
            # метку ставим в любом случае — не долбим при недоступном чате
            await pool.execute(
                "UPDATE business_connections SET stale_notified_at=now() "
                "WHERE connection_id=$1", r["connection_id"])
        elif not stale and notified is not None:
            # трафик вернулся — снимаем метку, чтобы предупредить при следующем зависании
            await pool.execute(
                "UPDATE business_connections SET stale_notified_at=NULL "
                "WHERE connection_id=$1", r["connection_id"])

    if sent:
        log.info("vault_watchdog: отправлено предупреждений: %d", sent)
    return sent
