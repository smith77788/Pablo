"""Тихие часы уведомлений и сводка за них.

Уведомления бота шли круглосуточно: итог ночной операции, новый подписчик,
сдвиг позиции будили владельца в три часа ночи. Выключить можно было только
тип целиком — и тогда терялись и дневные. У лидеров (Slack, amoCRM, ManyChat)
есть «не беспокоить» со сводкой после.

Как устроено:

* окно тишины — часы ``quiet_start``..``quiet_end`` в часовом поясе владельца
  (``notification_settings``; пояс мини-апп присылает из браузера, без него —
  Москва). Окно может переходить через полночь (23–8);
* в окне ``notify_if_enabled`` не отправляет несрочное, а кладёт текст в
  ``notification_held`` (см. :func:`should_hold`, :func:`hold`);
* срочное — ограничения и флуд аккаунтов — идёт сразу: это то, что нужно
  чинить, пока аккаунт не ушёл в бан (:data:`URGENT`);
* цикл :func:`run` раз в несколько минут отправляет каждому, у кого окно
  закончилось, ОДНО сообщение-сводку и чистит очередь.

Отказ модуля не глушит уведомление: при любой ошибке чтения настроек
уведомление уходит как раньше. Потерять ночью отчёт хуже, чем разбудить.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# Срочное идёт и ночью: ограничение аккаунта или флуд, оставленные до утра,
# — это аккаунт, потерянный к утру.
URGENT = frozenset({"restriction", "flood_warning"})

DEFAULT_TZ = "Europe/Moscow"
FLUSH_EVERY = 300          # секунд между обходами очереди
DIGEST_MAX_ITEMS = 15      # строк в сводке, дальше — «и ещё N»
DIGEST_LINE_MAX = 160      # символов на строку сводки
HELD_PER_USER_MAX = 500    # потолок очереди на владельца за одну ночь

PREF_LABELS = {
    "op_complete": "🧾 Операции",
    "new_user": "👤 Новые подписчики",
    "position_change": "📈 Позиции в поиске",
    "deploy": "🚀 Обновления",
}

PRESETS = ((22, 8), (23, 8), (0, 9))


def tz_or_default(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def valid_tz(name: Optional[str]) -> Optional[str]:
    """Имя пояса, если такой есть, иначе None (чужое значение не пишем)."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def in_quiet(now_utc: dt.datetime, start: int, end: int, tz: Optional[str]) -> bool:
    """Попадает ли момент в окно тишины [start, end) по местным часам.

    start == end — окна нет (иначе «тихо всегда» выглядело бы как настройка).
    """
    start, end = int(start) % 24, int(end) % 24
    if start == end:
        return False
    h = now_utc.astimezone(tz_or_default(tz)).hour
    if start < end:
        return start <= h < end
    return h >= start or h < end


def window_label(start: int, end: int) -> str:
    return f"{int(start):02d}:00–{int(end):02d}:00"


async def save(pool, uid: int, enabled: bool, start: int, end: int,
               tz: Optional[str] = None) -> None:
    """Записать тихие часы. Неизвестный пояс не пишется: остаётся прежний."""
    if not (0 <= int(start) <= 23 and 0 <= int(end) <= 23):
        raise ValueError("quiet hours must be 0..23")
    await pool.execute(
        "INSERT INTO notification_settings(user_id, quiet_enabled, quiet_start, "
        "quiet_end, tz) VALUES($1,$2,$3,$4,COALESCE($5,'Europe/Moscow')) "
        "ON CONFLICT(user_id) DO UPDATE SET quiet_enabled=$2, quiet_start=$3, "
        "quiet_end=$4, tz=COALESCE($5, notification_settings.tz), updated_at=now()",
        uid, bool(enabled), int(start), int(end), valid_tz(tz))


async def _settings(pool, uid: int):
    return await pool.fetchrow(
        "SELECT quiet_enabled, quiet_start, quiet_end, tz "
        "FROM notification_settings WHERE user_id=$1", uid)


async def should_hold(pool, uid: int, pref: str,
                      now_utc: Optional[dt.datetime] = None) -> bool:
    """Придержать ли уведомление до конца тихих часов."""
    if pref in URGENT:
        return False
    try:
        row = await _settings(pool, uid)
        # Строго True: «что-то истинное» от чужого пула или старой схемы не
        # должно глушить уведомления.
        if not row or row["quiet_enabled"] is not True:
            return False
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        return in_quiet(now_utc, int(row["quiet_start"]), int(row["quiet_end"]), row["tz"])
    except Exception:
        # Колонок нет (миграция v291 не накатилась) или база моргнула —
        # уведомление уходит как раньше.
        log.debug("quiet_hours: settings read failed uid=%s", uid, exc_info=True)
        return False


async def hold(pool, uid: int, pref: str, text: str) -> bool:
    """Положить уведомление в очередь сводки. False — не получилось (тогда
    вызывающий отправляет как обычно)."""
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM notification_held WHERE user_id=$1", uid)
        if (n or 0) >= HELD_PER_USER_MAX:
            # Очередь полна: сводка и так будет «и ещё много». Новое не
            # копим и не будим — это несрочное.
            log.info("quiet_hours: queue full uid=%s, %s dropped", uid, pref)
            return True
        await pool.execute(
            "INSERT INTO notification_held(user_id, pref, text) VALUES ($1, $2, $3)",
            uid, pref, text)
        return True
    except Exception:
        log.warning("quiet_hours: hold failed uid=%s pref=%s", uid, pref, exc_info=True)
        return False


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def plain_line(text: str) -> str:
    """Уведомление (HTML Telegram) — в одну строку сводки, без разметки."""
    s = html.unescape(_TAG.sub(" ", text or ""))
    s = _WS.sub(" ", s).strip()
    if len(s) > DIGEST_LINE_MAX:
        s = s[:DIGEST_LINE_MAX - 1].rstrip() + "…"
    return s


def build_digest(rows: list, window: str) -> str:
    """Сводка: по типам, свежие сверху, с честным счётом скрытого."""
    total = len(rows)
    by_pref: dict[str, list] = {}
    for r in rows:
        by_pref.setdefault(r["pref"], []).append(r)
    lines = [f"🌙 <b>Пока были тихие часы ({window})</b> — уведомлений: {total}"]
    shown = 0
    for pref, items in by_pref.items():
        label = PREF_LABELS.get(pref, "🔔 Прочее")
        lines.append(f"\n<b>{label}</b> · {len(items)}")
        for r in items[-5:]:
            if shown >= DIGEST_MAX_ITEMS:
                break
            t = r["created_at"]
            hhmm = t.strftime("%H:%M") if hasattr(t, "strftime") else ""
            lines.append(f"• {hhmm} {html.escape(plain_line(r['text']))}".rstrip())
            shown += 1
        if len(items) > 5:
            lines.append(f"  <i>…и ещё {len(items) - 5}</i>")
    if total > shown:
        lines.append(f"\n<i>Показано {shown} из {total}. Подробности — в разделах мини-аппа.</i>")
    return "\n".join(lines)


async def flush_once(pool, bot, now_utc: Optional[dt.datetime] = None) -> int:
    """Отправить сводки тем, у кого окно тишины закончилось. Возвращает число
    отправленных сводок."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    owners = await pool.fetch(
        "SELECT DISTINCT h.user_id, s.quiet_enabled, s.quiet_start, s.quiet_end, s.tz "
        "FROM notification_held h "
        "LEFT JOIN notification_settings s ON s.user_id = h.user_id")
    sent = 0
    for o in owners:
        uid = o["user_id"]
        if o["quiet_enabled"] and in_quiet(now_utc, o["quiet_start"], o["quiet_end"], o["tz"]):
            continue  # ещё ночь у этого владельца
        rows = await pool.fetch(
            "SELECT id, pref, text, created_at AT TIME ZONE $2 AS created_at "
            "FROM notification_held WHERE user_id=$1 ORDER BY id",
            uid, str(tz_or_default(o["tz"])))
        if not rows:
            continue
        window = window_label(o["quiet_start"] if o["quiet_start"] is not None else 23,
                              o["quiet_end"] if o["quiet_end"] is not None else 8)
        try:
            await bot.send_message(uid, build_digest(list(rows), window), parse_mode="HTML")
        except Exception as exc:
            from database.db import _notify_failure_is_permanent
            if not _notify_failure_is_permanent(exc):
                log.warning("quiet_hours: digest not sent uid=%s: %s", uid, str(exc)[:120])
                continue  # повторим на следующем обходе
            log.info("quiet_hours: digest dropped uid=%s (permanent): %s", uid, str(exc)[:120])
        await pool.execute(
            "DELETE FROM notification_held WHERE user_id=$1 AND id <= $2",
            uid, max(r["id"] for r in rows))
        sent += 1
    return sent


async def run(pool, bot) -> None:
    log.info("quiet_hours: starting")
    while True:
        try:
            await flush_once(pool, bot)
        except Exception:
            log.exception("quiet_hours: обход не удался")
        await asyncio.sleep(FLUSH_EVERY)
