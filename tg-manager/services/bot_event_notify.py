"""Уведомления владельцу о событиях его ботов: /start и заявки на вступление.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ. Владелец жаловался: «не всегда приходят уведомления,
когда кто-то запустил подключённого бота или подал заявку в канал». Причин
нашлось несколько, и все они жили в одном месте `auto_responder`:

1. Троттлинг «не чаще раза в минуту на бота» копил подписчиков, пришедших
   внутри окна, но отдавал накопленное ТОЛЬКО со следующим подписчиком после
   окна. Двое запустили бота за минуту, дальше тишина — про второго владелец
   не узнавал никогда (или узнавал через сутки, вместе с чужим).
2. Поверх этого троттлинга `notify_if_enabled` держал СВОЙ минутный кулдаун на
   тот же ключ (`{bot_id}:new`). Два окна по 60 с, отсчитанные от чуть разных
   моментов: уведомление, разрешённое первым, второе молча глотало (возвращая
   «доставлено»), и счётчик накопленных пропадал вместе с ним.
3. Уведомлялся только ПЕРВЫЙ в истории контакт человека с ботом. Тот, кто
   писал боту когда-то раньше и сегодня снова нажал /start, не порождал ничего.
4. Заявок на вступление продукт не слушал вовсе: `chat_join_request` не было
   ни в `allowed_updates` главного бота, ни у подключённых ботов, и
   обработчика не было ни одного.

Здесь — накопитель, который НИЧЕГО не теряет: первое событие после паузы
уходит сразу, следующие внутри окна копятся и уходят одним сообщением РОВНО по
окончании окна (таймер), а не «когда придёт кто-то ещё». Защита от накрутки
(не больше одного сообщения в минуту на источник) при этом сохраняется.

Куда уходит: в каналы-приёмники владельца (services/bot_notify_routes.py —
отдельный канал на каждую сеть проектов) и в личку, если её не выключили.
"""
from __future__ import annotations

import asyncio
import html
import itertools
import logging
import time
from typing import Any, Awaitable, Callable

from services import bot_notify_routes as _routes

log = logging.getLogger(__name__)

# Не чаще одного уведомления в окно на источник (бот или чат) — анти-накрутка:
# поток фейков не должен класть главного бота в FloodWait.
WINDOW_S = 60.0
# Сколько имён перечислять в сводке, дальше — «и ещё N».
NAMES_MAX = 5
# С какого числа новых подписчиков за окно предупреждать о возможной накрутке.
FLOOD_HINT_AT = 10
# Повторный /start от знакомого человека считаем «вернулся», если он молчал
# хотя бы столько. Иначе каждый /start активного пользователя был бы событием.
RETURN_AFTER_S = 24 * 3600

# Уникальный хвост ключа анти-повтора для notify_if_enabled: окно держим МЫ,
# второй кулдаун на том же ключе съедал уведомления (причина 2 выше).
_seq = itertools.count(1)

Sender = Callable[[str, Any], Awaitable[Any]]


class _Bucket:
    __slots__ = ("last", "events", "task")

    def __init__(self) -> None:
        self.last = float("-inf")  # первое событие источника — сразу
        self.events: list[dict] = []
        self.task: asyncio.Task | None = None


_buckets: dict[str, _Bucket] = {}
# Сильные ссылки на таймеры сводок до их завершения.
_timers: set[asyncio.Task] = set()


def take(key: str, event: dict, now: float | None = None) -> list[dict] | None:
    """Положить событие. Вернуть пачку, если её надо отправить СЕЙЧАС.

    None — событие придержано: оно уйдёт сводкой по таймеру (`due_in`).
    ЧИСТАЯ по эффекту (только память процесса).
    """
    now = time.monotonic() if now is None else now
    b = _buckets.setdefault(key, _Bucket())
    b.events.append(event)
    if now - b.last >= WINDOW_S and not (b.task and not b.task.done()):
        batch, b.events = b.events, []
        b.last = now
        return batch
    return None


def due_in(key: str, now: float | None = None) -> float:
    """Через сколько секунд кончится окно источника (0 — уже)."""
    now = time.monotonic() if now is None else now
    b = _buckets.get(key)
    if b is None or b.last == float("-inf"):
        return 0.0
    return max(0.0, b.last + WINDOW_S - now)


def flush(key: str, now: float | None = None) -> list[dict]:
    """Забрать накопленное (для таймера). Отметить момент отправки."""
    now = time.monotonic() if now is None else now
    b = _buckets.get(key)
    if b is None or not b.events:
        return []
    batch, b.events = b.events, []
    b.last = now
    return batch


def pending(key: str) -> int:
    b = _buckets.get(key)
    return len(b.events) if b else 0


def submit(key: str, event: dict, send: Callable[[list[dict]], Awaitable[Any]]) -> None:
    """Принять событие и гарантировать его доставку: сразу или сводкой.

    `send(batch)` зовётся либо немедленно (фоновой задачей), либо таймером по
    окончании окна. Таймер на источник один: повторные события его не плодят.
    """
    batch = take(key, event)
    if batch is not None:
        _spawn(send(batch), f"notify:{key}")
        return
    b = _buckets[key]
    if b.task is not None and not b.task.done():
        return

    async def _later() -> None:
        try:
            await asyncio.sleep(due_in(key))
            rest = flush(key)
            if rest:
                await send(rest)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bot_event_notify: сводка не отправлена key=%s", key)

    b.task = _spawn(_later(), f"notify-later:{key}")


def _spawn(coro, name: str) -> asyncio.Task | None:
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError:
        coro.close()
        return None
    _timers.add(task)
    task.add_done_callback(_timers.discard)
    return task


def dedup_key(prefix: str) -> str:
    """Ключ для notify_if_enabled, у которого НЕТ общего слота с соседями."""
    return f"{prefix}:{next(_seq)}"


# ── Тексты ─────────────────────────────────────────────────────────────────

def _who(user: dict | None, fallback_id: Any = None) -> str:
    """«@ник» или имя, экранированное под HTML. Имя — данные из Telegram."""
    user = user or {}
    uname = (user.get("username") or "").strip()
    if uname:
        return "@" + html.escape(uname)
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    if name:
        return html.escape(name[:64])
    uid = user.get("id") or fallback_id
    return f"id{uid}" if uid else "без имени"


def start_event(user: dict | None, chat_id: int, *, new: bool) -> dict:
    return {"kind": "new" if new else "back", "who": _who(user, chat_id)}


def start_note(bot_name: str, batch: list[dict]) -> str:
    """Текст уведомления о запусках бота. ЧИСТАЯ."""
    bot = "@" + html.escape(bot_name.lstrip("@")) if bot_name else "вашего бота"
    if len(batch) == 1:
        e = batch[0]
        if e.get("kind") == "back":
            return f"🔁 <b>Снова запустил бота</b> {e['who']} → {bot}"
        return f"👤 <b>Новый пользователь</b> {e['who']} запустил {bot}"
    new = sum(1 for e in batch if e.get("kind") == "new")
    back = len(batch) - new
    parts = []
    if new:
        parts.append(f"{new} {_plural(new, 'новый', 'новых', 'новых')}")
    if back:
        parts.append(f"{back} {_plural(back, 'вернулся', 'вернулись', 'вернулись')}")
    names = ", ".join(e["who"] for e in batch[:NAMES_MAX])
    more = f" и ещё {len(batch) - NAMES_MAX}" if len(batch) > NAMES_MAX else ""
    text = (f"👤 <b>{bot} запустили {len(batch)} "
            f"{_plural(len(batch), 'раз', 'раза', 'раз')}</b> за минуту "
            f"({', '.join(parts)}).\n{names}{more}")
    if new >= FLOOD_HINT_AT:
        text += ("\n⚠️ Если вы не запускали продвижение — возможно, идёт "
                 "накрутка ботами.")
    return text


def join_event(cjr: dict) -> dict:
    """Событие заявки из апдейта Bot API `chat_join_request`."""
    user = cjr.get("from") or {}
    link = (cjr.get("invite_link") or {})
    return {
        "user_id": int(user.get("id") or 0),
        "who": _who(user),
        "bio": html.escape((cjr.get("bio") or "").strip()[:120]),
        "link": html.escape((link.get("name") or "").strip()[:40]),
    }


def chat_label(chat: dict | None) -> str:
    chat = chat or {}
    title = (chat.get("title") or "").strip()
    if title:
        return "«" + html.escape(title[:64]) + "»"
    if chat.get("username"):
        return "@" + html.escape(chat["username"])
    return "чат " + str(chat.get("id") or "")


def join_note(chat: dict | None, batch: list[dict], via_bot: str = "") -> str:
    """Текст уведомления о заявках на вступление. ЧИСТАЯ."""
    where = chat_label(chat)
    tail = f"\n<i>через бота @{html.escape(via_bot.lstrip('@'))}</i>" if via_bot else ""
    if len(batch) == 1:
        e = batch[0]
        text = f"📨 <b>Заявка на вступление</b> в {where}\nОт: {e['who']}"
        if e.get("bio"):
            text += f"\nО себе: {e['bio']}"
        if e.get("link"):
            text += f"\nПо ссылке: {e['link']}"
        return text + tail
    names = ", ".join(e["who"] for e in batch[:NAMES_MAX])
    more = f" и ещё {len(batch) - NAMES_MAX}" if len(batch) > NAMES_MAX else ""
    return (f"📨 <b>{len(batch)} {_plural(len(batch), 'заявка', 'заявки', 'заявок')}"
            f" на вступление</b> в {where} за минуту\n{names}{more}\n"
            f"Одобрить можно в Telegram: «Управление» → «Заявки на вступление».{tail}")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


# ── Владелец чата главного бота ────────────────────────────────────────────

def channel_id_variants(chat_id: int) -> list[int]:
    """id чата в обоих видах: Bot API (-100…) и MTProto (голый положительный).

    `managed_channels.channel_id` пишут аккаунты (Telethon) — там голый id,
    а апдейт бота приносит -100…. Ищем по обоим.
    """
    out = [int(chat_id)]
    raw = str(abs(int(chat_id)))
    if int(chat_id) < 0 and raw.startswith("100") and len(raw) > 10:
        out.append(int(raw[3:]))
    elif int(chat_id) > 0:
        out.append(int("-100" + raw))
    return out


async def owners_of_chat(pool, chat_id: int) -> list[int]:
    """Кому из владельцев сообщать о событии в чате, где админ ГЛАВНЫЙ бот.

    Сначала «Модератор чатов» (`guard_chats.owner_id` — кто добавил бота), затем
    каталог каналов (`managed_channels` — чьи аккаунты ведут этот канал).
    """
    ids = channel_id_variants(chat_id)
    owners: list[int] = []
    try:
        row = await pool.fetchrow(
            "SELECT owner_id FROM guard_chats WHERE chat_id = ANY($1::bigint[])", ids)
        if row and int(row["owner_id"] or 0) > 0:
            owners.append(int(row["owner_id"]))
    except Exception:
        log.debug("owners_of_chat: guard_chats", exc_info=True)
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT owner_id FROM managed_channels "
            "WHERE channel_id = ANY($1::bigint[])", ids)
        for r in rows or ():
            o = int(r["owner_id"] or 0)
            if o > 0 and o not in owners:
                owners.append(o)
    except Exception:
        log.debug("owners_of_chat: managed_channels", exc_info=True)
    return owners


# ── Кнопки «Одобрить / Отклонить» ──────────────────────────────────────────

def join_buttons(bot_id: int, chat_id: int, user_id: int):
    """Клавиатура под уведомлением об ОДНОЙ заявке (bot_id=0 — главный бот)."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from bot.callbacks import JoinReqCb

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=JoinReqCb(
        action="a", bot_id=int(bot_id), chat_id=int(chat_id), user_id=int(user_id)))
    kb.button(text="✖️ Отклонить", callback_data=JoinReqCb(
        action="d", bot_id=int(bot_id), chat_id=int(chat_id), user_id=int(user_id)))
    kb.adjust(2)
    return kb.as_markup()


def join_error_ru(desc: str) -> str:
    """Отказ Telegram на одобрение/отклонение — по-русски."""
    d = (desc or "").upper()
    if "HIDE_REQUESTER_MISSING" in d or "REQUEST" in d and "NOT FOUND" in d:
        return "Заявки уже нет: её обработали или человек её отозвал."
    if "USER_ALREADY_PARTICIPANT" in d:
        return "Человек уже состоит в чате."
    if "NOT ENOUGH RIGHTS" in d or "CHAT_ADMIN_REQUIRED" in d or "RIGHT" in d:
        return "У бота нет права «Добавление участников» в этом чате."
    if "CHANNELS_TOO_MUCH" in d:
        return "Человек состоит в слишком большом числе каналов."
    return "Telegram не принял действие. Попробуйте в самом Telegram."


async def notify_join_request(pool, main_bot, owner_id: int, *, bot_id: int,
                              cjr: dict, via_bot: str = "") -> None:
    """Принять заявку на вступление и гарантированно сообщить владельцу.

    Источник окна — пара (владелец, чат): волна заявок после рекламы уходит
    одной сводкой в минуту, а одиночная заявка — сразу и с кнопками.
    """
    from database import db

    chat = cjr.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    if not owner_id or not chat_id or main_bot is None:
        return
    key = f"jr:{owner_id}:{chat_id}"

    async def _send(batch: list[dict]) -> None:
        markup = None
        if len(batch) == 1 and batch[0].get("user_id"):
            try:
                markup = join_buttons(bot_id, chat_id, batch[0]["user_id"])
            except Exception:
                log.debug("join buttons", exc_info=True)
        text = join_note(chat, batch, via_bot)
        if await _routes.deliver(pool, main_bot, owner_id, bot_id=bot_id,
                                 event="join_request", text=text, reply_markup=markup):
            return
        await db.notify_if_enabled(
            pool, main_bot, owner_id, "new_user", text,
            reply_markup=markup, dedup_key=dedup_key(key))

    submit(key, join_event(cjr), _send)


async def notify_start(pool, main_bot, owner_id: int, *, bot_id: int,
                       bot_name: str, event: dict) -> None:
    """Сообщить владельцу о запуске его бота: сразу или сводкой за минуту."""
    from database import db

    if not owner_id or main_bot is None:
        return
    key = f"start:{bot_id}"

    async def _send(batch: list[dict]) -> None:
        text = start_note(bot_name, batch)
        if await _routes.deliver(pool, main_bot, owner_id, bot_id=bot_id,
                                 event="start", text=text):
            return
        await db.notify_if_enabled(
            pool, main_bot, owner_id, "new_user", text, dedup_key=dedup_key(key))

    submit(key, event, _send)
