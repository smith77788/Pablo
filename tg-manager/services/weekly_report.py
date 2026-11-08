"""Еженедельный отчёт владельцу — сам приходит в бот по понедельникам.

ЗАЧЕМ. Пульс-дайджест (services.organism.digest) собирал цельную картину
«что изменилось и что делать», но отдавался только по запросу мини-аппа:
владелец, который не открыл приложение, не узнавал ни о росте, ни о падении.
У лидеров рынка сводка приходит сама. Здесь — раз в неделю, утром понедельника,
в личку: главное одной строкой, ключевые числа с изменением к прошлому отчёту,
заказы и выручка бота-продавца, три самых важных действия.

КОГДА. Окно — понедельник 06:00–12:00 UTC (09:00–15:00 МСК): сердцебиение
организма идёт раз в 15 минут, окно с запасом переживает рестарт. Повтор держит
`db.notify_dedup_ok` с ключом недели — переживает рестарт и не шлёт второй
отчёт за ту же неделю.

ВЫКЛЮЧИТЬ. Кнопка под отчётом — тот же «Больше не напоминать» организма
(SnoozeCb, список dismissed), поэтому возврат — кнопкой «Вернуть» или /unmute.

Тренды считаются от прошлого ОТЧЁТА (своя база в organism_state), а не от
последнего открытия экрана в мини-аппе.

СВОДКА ЗА ДЕНЬ. У лидеров дневная сводка — по желанию. Здесь — кнопка
«📅 Присылать каждый день» под еженедельным отчётом и команда /summary (сводка
прямо сейчас). Включённая сводка приходит каждое утро, кроме понедельника (в
понедельник приходит недельный отчёт), с трендами к вчерашней сводке — своя база
`digest_metrics_daily`. По умолчанию выключена: в личку без спроса — только
раз в неделю.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

log = logging.getLogger(__name__)

REPORT_ID = "weekly_report"
_STATE_KEY = "digest_metrics_weekly"
_WINDOW_HOURS = (6, 12)
_DEDUP_S = 6 * 86400

DAILY_ID = "daily_report"          # ключ включателя в organism_state
_DAILY_STATE_KEY = "digest_metrics_daily"
_DAILY_DEDUP_S = 20 * 3600

_SALES_SQL = """
    SELECT currency,
           count(*) FILTER (WHERE created_at >= now() - make_interval(days => $2)) AS orders,
           count(*) FILTER (WHERE created_at >= now() - make_interval(days => $2)
                            AND status IN ('confirmed','done')) AS paid,
           COALESCE(sum(total_cents) FILTER (WHERE created_at >= now() - make_interval(days => $2)
                            AND status IN ('confirmed','done')), 0) AS revenue,
           COALESCE(sum(total_cents) FILTER (WHERE created_at < now() - make_interval(days => $2)
                            AND status IN ('confirmed','done')), 0) AS revenue_prev
      FROM bot_sales_orders
     WHERE owner_id = $1 AND created_at >= now() - make_interval(days => $2 * 2)
     GROUP BY currency
"""

# Сценарии за неделю: сколько людей зашло, дошло до цели, сломалось — и
# какой сценарий самый результативный. Лучший — по целям, затем по
# завершённым: без цели сценарий тоже может быть полезен.
_SCENARIOS_SQL = """
    SELECT d.id, d.name,
           count(r.id) AS started,
           count(r.id) FILTER (WHERE r.goal_reached_at IS NOT NULL) AS goals,
           count(r.id) FILTER (WHERE r.status = 'completed') AS completed,
           count(r.id) FILTER (WHERE r.status = 'failed') AS failed
      FROM workflow_runs r
      JOIN workflow_definitions d ON d.id = r.workflow_id
     WHERE r.owner_id = $1 AND r.started_at >= now() - make_interval(days => $2)
     GROUP BY d.id, d.name
     ORDER BY goals DESC, completed DESC, started DESC
"""

_CUR_SIGN = {"USD": "$", "RUB": "₽", "EUR": "€", "UAH": "₴", "KZT": "₸"}


def in_window(now: datetime) -> bool:
    """Понедельник, утро по UTC. ЧИСТАЯ."""
    return now.weekday() == 0 and _WINDOW_HOURS[0] <= now.hour < _WINDOW_HOURS[1]


def in_daily_window(now: datetime) -> bool:
    """Утро любого дня, кроме понедельника (тогда приходит недельный). ЧИСТАЯ."""
    return now.weekday() != 0 and _WINDOW_HOURS[0] <= now.hour < _WINDOW_HOURS[1]


def day_key(now: datetime) -> str:
    return f"{DAILY_ID}:{now.date().isoformat()}"


def week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{REPORT_ID}:{iso[0]}-W{iso[1]:02d}"


def _money(cents: int, currency: str) -> str:
    value = f"{int(cents) / 100:,.0f}".replace(",", " ")
    sign = _CUR_SIGN.get((currency or "").upper())
    return f"{sign}{value}" if sign in ("$", "€") else f"{value} {sign or escape(currency or '')}"


def _delta(trend: dict | None) -> str:
    if not trend or not trend.get("delta"):
        return ""
    d = int(trend["delta"])
    mark = "🟢" if trend.get("good") else "🔴"
    return f" {mark} {'+' if d > 0 else '−'}{abs(d)}"


def format_report(digest: dict, sales: list[dict] | None = None,
                  scenarios: list[dict] | None = None, *, daily: bool = False) -> str:
    """Отчёт в HTML для Telegram. ЧИСТАЯ: на входе дайджест и строки продаж.

    daily=True — сводка за день: те же блоки, сравнение со вчера."""
    title = "📊 <b>Сводка за день</b>" if daily else "📊 <b>Отчёт за неделю</b>"
    prev_word = "к вчера" if daily else "к прошлой неделе"
    lines = [title, "", escape(digest.get("headline") or "")]

    if sales:
        lines += ["", "💰 <b>Продажи бота-продавца</b>"]
        for row in sales:
            cur = str(row.get("currency") or "USD")
            rev, prev = int(row.get("revenue") or 0), int(row.get("revenue_prev") or 0)
            trend = ""
            if prev:
                pct = round((rev - prev) * 100 / prev)
                trend = f" ({'+' if pct >= 0 else '−'}{abs(pct)}% {prev_word})"
            lines.append(
                f"Заказов: <b>{int(row.get('orders') or 0)}</b>, оплачено: "
                f"<b>{int(row.get('paid') or 0)}</b>, выручка: <b>{_money(rev, cur)}</b>{trend}")

    if scenarios:
        tot = {k: sum(int(r.get(k) or 0) for r in scenarios)
               for k in ("started", "goals", "completed", "failed")}
        lines += ["", "🔄 <b>Сценарии</b>",
                  f"Зашли: <b>{tot['started']}</b>, дошли до цели: <b>{tot['goals']}</b>, "
                  f"прошли до конца: <b>{tot['completed']}</b>"
                  + (f", с ошибкой: <b>{tot['failed']}</b>" if tot["failed"] else "")]
        best = scenarios[0]
        if int(best.get("goals") or 0) or int(best.get("completed") or 0):
            lines.append(f"Лучший: «{escape(str(best.get('name') or ''))}» — "
                         f"{int(best.get('goals') or 0)} целей из "
                         f"{int(best.get('started') or 0)}")

    for sec in digest.get("sections") or []:
        stats = [s for s in sec.get("stats") or [] if int(s.get("value") or 0)
                 or (s.get("trend") or {}).get("delta")]
        if not stats:
            continue
        lines += ["", f"<b>{escape(sec.get('title') or '')}</b>"]
        for s in stats:
            lines.append(f"{escape(s.get('label') or '')}: <b>{int(s.get('value') or 0)}</b>"
                         f"{_delta(s.get('trend'))}")

    recs = (digest.get("recommendations") or [])[:3]
    if recs:
        lines += ["", "👉 <b>Что сделать сегодня</b>" if daily
                  else "👉 <b>Что сделать на этой неделе</b>"]
        for i, r in enumerate(recs, 1):
            why = f" — {escape(r['why'])}" if r.get("why") else ""
            lines.append(f"{i}. {escape(r.get('title') or '')}{why}")

    if not digest.get("has_prev"):
        lines += ["", "<i>Это первая сводка: с завтрашней появятся изменения к вчера.</i>"
                  if daily else
                  "<i>Это первый отчёт: со следующей недели появятся изменения к прошлой.</i>"]
    return "\n".join(lines)


async def sales_week(pool, owner_id: int, days: int = 7) -> list[dict]:
    """Заказы и выручка бота-продавца за `days` дней по валютам. Пусто — продаж нет."""
    try:
        rows = await pool.fetch(_SALES_SQL, owner_id, days)
    except Exception:
        log.debug("weekly_report: продажи не прочитаны owner=%s", owner_id, exc_info=True)
        return []
    return [dict(r) for r in rows if int(r["orders"] or 0) or int(r["revenue_prev"] or 0)]


async def scenarios_week(pool, owner_id: int, days: int = 7) -> list[dict]:
    """Прогоны сценариев за `days` дней по сценариям. Пусто — никто не заходил."""
    try:
        rows = await pool.fetch(_SCENARIOS_SQL, owner_id, days)
    except Exception:
        log.debug("weekly_report: сценарии не прочитаны owner=%s", owner_id, exc_info=True)
        return []
    return [dict(r) for r in rows]


def _kb(daily_on: bool = False):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import SnoozeCb

    kb = InlineKeyboardBuilder()
    kb.button(text="🔕 Не присылать отчёт",
              callback_data=SnoozeCb(action="off", sid=REPORT_ID, code="never"))
    kb.button(text="📅 Не присылать каждый день" if daily_on else "📅 Присылать каждый день",
              callback_data=SnoozeCb(action="daily", sid=DAILY_ID,
                                     code="off" if daily_on else "on"))
    kb.adjust(1)
    return kb.as_markup()


async def daily_enabled(pool, owner_id: int) -> bool:
    from services.organism import spine
    try:
        return bool(await spine.state_get(pool, owner_id, DAILY_ID, False))
    except Exception:
        return False


async def set_daily(pool, owner_id: int, on: bool) -> None:
    from services.organism import spine
    await spine.state_set(pool, owner_id, DAILY_ID, bool(on))


async def build_daily(pool, owner_id: int) -> str:
    """Текст сводки за день (для утренней рассылки и /summary)."""
    from services.organism import digest

    report = await digest.build(pool, owner_id, state_key=_DAILY_STATE_KEY)
    return format_report(report, await sales_week(pool, owner_id, 1),
                         await scenarios_week(pool, owner_id, 1), daily=True)


async def maybe_send_daily(pool, bot, owner_id: int, *, now: datetime | None = None) -> bool:
    """Утренняя сводка за день — только если владелец её включил. True — отправлена."""
    from database import db

    now = now or datetime.now(UTC)
    if not in_daily_window(now):
        return False
    if not await daily_enabled(pool, owner_id):
        return False
    key = day_key(now)
    if not await db.notify_dedup_ok(pool, owner_id, key, _DAILY_DEDUP_S):
        return False
    try:
        text = await build_daily(pool, owner_id)
        await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=_kb(True),
                               disable_web_page_preview=True)
    except Exception:
        log.warning("weekly_report: сводка за день не отправлена owner=%s", owner_id,
                    exc_info=True)
        try:
            await db.notify_dedup_forget(pool, owner_id, key)
        except Exception:
            pass
        return False
    return True


async def maybe_send(pool, bot, owner_id: int, *, now: datetime | None = None) -> bool:
    """Отправить отчёт недели, если пора и ещё не отправлен. True — отправлен."""
    from database import db

    from services.organism import digest, spine

    now = now or datetime.now(UTC)
    if not in_window(now):
        return False
    try:
        dismissed = await spine.state_get(pool, owner_id, "dismissed", []) or []
    except Exception:
        dismissed = []
    if REPORT_ID in dismissed:
        return False
    key = week_key(now)
    if not await db.notify_dedup_ok(pool, owner_id, key, _DEDUP_S):
        return False
    try:
        report = await digest.build(pool, owner_id, state_key=_STATE_KEY)
        text = format_report(report, await sales_week(pool, owner_id),
                             await scenarios_week(pool, owner_id))
        await bot.send_message(owner_id, text, parse_mode="HTML",
                               reply_markup=_kb(await daily_enabled(pool, owner_id)),
                               disable_web_page_preview=True)
    except Exception:
        log.warning("weekly_report: не отправлен owner=%s", owner_id, exc_info=True)
        # Отметку недели снимаем: следующий такт в том же окне попробует снова.
        try:
            await db.notify_dedup_forget(pool, owner_id, key)
        except Exception:
            pass
        return False
    return True

