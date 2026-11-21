"""Напоминания «подключите Хранилище диалогов» в личку новичку.

Хранилище — одна из главных фишек продукта, и тот, кто прошёл мимо шага в
«Быстром старте», мог так и не узнать о ней. Бот напоминает РОВНО ДВАЖДЫ:
на второй день после регистрации и на седьмой (не раньше чем через пять дней
после первого напоминания). Подключил — напоминаний больше нет; два ушло —
больше нет никогда. Дальше Хранилище предлагает только подсказка на главной,
которую можно отложить.

Только новички (зарегистрированы не раньше NEW_USER_WINDOW_DAYS назад): выкатка
не должна разом написать всей старой базе.

Счётчик занимается ДО отправки условным UPDATE (claim): процесс, умерший между
отправкой и записью, иначе прислал бы то же напоминание второй раз. Отправка
идёт через общий слой notify_if_enabled (тумблер «Советы»), то есть владелец,
отключивший советы, напоминаний не получит.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

FIRST_AFTER_DAYS = 2           # первое напоминание — на второй день
SECOND_AFTER_DAYS = 7          # второе — на седьмой
MIN_GAP_DAYS = 5               # и не раньше чем через пять дней после первого
MAX_NUDGES = 2
NEW_USER_WINDOW_DAYS = 30      # старой базе выкатка не пишет
BATCH = 200                    # за проход — не больше (лимиты Telegram)
CHECK_EVERY = 6 * 3600

TEXTS = (
    "🗄 <b>Ваша переписка может пропасть</b>\n\n"
    "Собеседник удаляет сообщение или весь диалог — и у вас не остаётся ничего. "
    "<b>Хранилище диалогов</b> это исправляет: бот сохраняет все ваши личные "
    "переписки, показывает удалённые и изменённые сообщения и сообщает, когда "
    "кто-то что-то удалил.\n\n"
    "Подключение — минута: Настройки Telegram → <b>Telegram для бизнеса</b> → "
    "<b>Чат-боты</b> → добавьте этого бота.",

    "🗄 <b>Хранилище диалогов всё ещё не подключено</b>\n\n"
    "Пока оно выключено, удалённые собеседниками сообщения исчезают навсегда. "
    "Подключите — и бот сохранит всю переписку, а поиск и ответ будут прямо "
    "из архива.\n\n"
    "Это последнее напоминание: дальше Хранилище будет ждать вас на главной.",
)

# Кому пора: зарегистрирован недавно, Хранилище не подключал ни разу (даже
# отключённое подключение — уже не «не знает о фишке»), напоминаний меньше
# двух, и подошёл срок следующего.
_DUE_SQL = """
SELECT pu.user_id, COALESCE(n.sent_count, 0) AS sent_count
  FROM platform_users pu
  LEFT JOIN vault_nudges n ON n.owner_id = pu.user_id
 WHERE NOT COALESCE(pu.is_banned, false)
   AND COALESCE(pu.registered_at, pu.first_seen) > now() - make_interval(days => $1)
   AND NOT EXISTS (SELECT 1 FROM business_connections bc WHERE bc.owner_id = pu.user_id)
   AND COALESCE(n.sent_count, 0) < $2
   AND (
        (COALESCE(n.sent_count, 0) = 0
         AND COALESCE(pu.registered_at, pu.first_seen) <= now() - make_interval(days => $3))
     OR (n.sent_count = 1
         AND COALESCE(pu.registered_at, pu.first_seen) <= now() - make_interval(days => $4)
         AND n.last_sent_at <= now() - make_interval(days => $5))
   )
 ORDER BY pu.user_id
 LIMIT $6
"""

# Занять следующий номер напоминания. Возвращает строку, только если именно
# этот проход сдвинул счётчик с ``expected`` — второй процесс не пройдёт.
_CLAIM_SQL = """
INSERT INTO vault_nudges AS n (owner_id, sent_count, last_sent_at)
VALUES ($1, 1, now())
ON CONFLICT (owner_id) DO UPDATE
   SET sent_count = n.sent_count + 1, last_sent_at = now()
 WHERE n.sent_count = $2
RETURNING n.sent_count
"""


def _markup():
    """Кнопка в окно подключения мини-аппа; без мини-аппа — экран /vault."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from bot.callbacks import VaultCb
    kb = InlineKeyboardBuilder()
    try:
        from aiogram.types import WebAppInfo

        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL
        url = _valid_mini_app_url(MINI_APP_URL)
    except Exception:
        url = None
    if url:
        kb.button(text="🗄 Подключить Хранилище",
                  web_app=WebAppInfo(url=f"{url}#vault_connect"))
    kb.button(text="📖 Как подключить", callback_data=VaultCb(a="how"))
    kb.adjust(1)
    return kb.as_markup()


async def _claim(pool: asyncpg.Pool, uid: int, expected: int) -> bool:
    row = await pool.fetchrow(_CLAIM_SQL, uid, expected)
    return row is not None and int(row["sent_count"]) == expected + 1


async def check_once(pool: asyncpg.Pool, bot: Bot) -> int:
    """Один проход. Возвращает число отправленных напоминаний."""
    rows = await pool.fetch(_DUE_SQL, NEW_USER_WINDOW_DAYS, MAX_NUDGES,
                            FIRST_AFTER_DAYS, SECOND_AFTER_DAYS, MIN_GAP_DAYS, BATCH)
    if not rows:
        return 0
    from database import db as _db
    markup = _markup()
    sent = 0
    for r in rows:
        uid, n = int(r["user_id"]), int(r["sent_count"])
        if n >= MAX_NUDGES:
            continue
        try:
            if not await _claim(pool, uid, n):
                continue   # другой процесс уже занял это напоминание
        except Exception:
            log.warning("vault_nudge: claim failed uid=%s", uid, exc_info=True)
            continue
        try:
            await _db.notify_if_enabled(pool, bot, uid, "advice", TEXTS[n],
                                        reply_markup=markup,
                                        dedup_key=f"vault_nudge_{n + 1}")
            sent += 1
        except Exception:
            log.debug("vault_nudge: не удалось отправить uid=%s", uid, exc_info=True)
        await asyncio.sleep(0.05)
    if sent:
        log.info("vault_nudge: отправлено напоминаний: %d", sent)
    return sent


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("vault_nudge: starting")
    while True:
        try:
            await check_once(pool, bot)
        except Exception:
            log.exception("vault_nudge: check failed")
        await asyncio.sleep(CHECK_EVERY)
