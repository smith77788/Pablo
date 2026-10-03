"""Фоновый цикл виртуального администратора каналов — «штат», который работает сам.

Каждую минуту, по всем каналам, где администратор включён:
  1. настройка: профиль, рубрики и контент-план для только что поставленных;
  2. исход прошлой публикации: не прошла — самолечение (отступ, повтор, и только
     на повторяющийся сбой — сообщение владельцу);
  3. публикация по расписанию (services/channel_admin.tick_post);
  4. контент-план достраивается наперёд, когда кончается;
  5. статистика раз в 6 часов: просмотры своих постов и подписчики, по ним
     подстраиваются доли рубрик;
  6. раз в сутки — отчёт владельцу по всем его каналам.
Черновики, которые владелец не разобрал за двое суток, снимаются.

Каждый шаг отдельного канала изолирован: сбой одного канала не останавливает
остальные, сбой такта не останавливает цикл.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from services import channel_admin as ca

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 60.0
_BATCH = 20  # каналов на шаг за такт: ИИ и Telegram медленные, не держим цикл


async def _rows(pool, where: str, *args) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {ca._ADMIN_COLS} FROM va_channel_admin WHERE enabled AND {where} "
        f"LIMIT {_BATCH}", *args)
    return [dict(r) for r in rows or []]


async def _safe(label: str, coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("channel_admin_runner: %s упал", label)


async def _claim(pool, admin: dict, now: datetime) -> bool:
    """Занять такт канала: сдвинуть next_post_at, только если его никто не сдвинул."""
    row = await pool.fetchrow(
        "UPDATE va_channel_admin SET next_post_at = now() + interval '20 minutes' "
        "WHERE id=$1 AND next_post_at IS NOT DISTINCT FROM $2 RETURNING id",
        admin["id"], admin["next_post_at"])
    return bool(row)


async def run_once(pool, bot, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"setup": 0, "posted": 0, "planned": 0, "stats": 0, "reports": 0}
    await _safe("expire drafts", pool.execute(
        "UPDATE va_admin_drafts SET status='expired', decided_at=now() "
        "WHERE status='pending' AND created_at < now() - make_interval(hours => $1)",
        ca._DRAFT_TTL_H))

    for a in await _rows(pool, "NOT setup_done AND (last_error IS NULL OR "
                               "updated_at < now() - interval '30 minutes')"):
        try:
            await ca.setup_channel(pool, a["owner_id"], a["channel_id"])
            stats["setup"] += 1
        except Exception as e:
            reason = str(e) if isinstance(e, ca.ChannelAdminError) else f"настройка не удалась: {e}"
            await _safe("setup fail", ca._fail(pool, bot, a, reason[:300],
                                               retry_in=_td(minutes=30)))

    for a in await _rows(pool, "setup_done AND last_op_id IS NOT NULL"):
        await _safe("check publish", ca.check_last_publish(pool, bot, a))

    for a in await _rows(pool, "setup_done AND next_post_at IS NOT NULL AND next_post_at <= $1", now):
        if not await _claim(pool, a, now):
            continue
        try:
            res = await ca.tick_post(pool, bot, a, now=now)
            if res in ("published", "draft"):
                stats["posted"] += 1
        except Exception:
            log.exception("channel_admin_runner: такт канала %s упал", a["channel_id"])

    for a in await _rows(
            pool, "setup_done AND ("
                  "NOT EXISTS (SELECT 1 FROM va_admin_plan p WHERE "
                  "p.owner_id=va_channel_admin.owner_id AND p.channel_id=va_channel_admin.channel_id "
                  "AND p.status='planned' AND p.slot_at > now() + make_interval(hours => $1)) OR "
                  "EXISTS (SELECT 1 FROM va_admin_plan p WHERE "
                  "p.owner_id=va_channel_admin.owner_id AND p.channel_id=va_channel_admin.channel_id "
                  "AND p.status='planned' AND p.slot_at > now() + make_interval(days => $2)) OR "
                  "(SELECT count(*) FROM va_admin_plan p WHERE "
                  "p.owner_id=va_channel_admin.owner_id AND p.channel_id=va_channel_admin.channel_id "
                  "AND p.status='planned' AND p.slot_at <= now() + make_interval(days => $2)) > "
                  "LEAST(GREATEST(COALESCE(va_channel_admin.posts_per_day,1),1)*$2,$3))",
            ca._PLAN_MIN_AHEAD_H, ca._PLAN_DAYS, ca._PLAN_MAX_SLOTS):
        try:
            stats["planned"] += await ca.ensure_plan(pool, a["owner_id"], a["channel_id"], now=now)
        except Exception:
            log.exception("channel_admin_runner: план канала %s", a["channel_id"])

    for a in await _rows(pool, "setup_done AND (last_stats_at IS NULL OR "
                               "last_stats_at < now() - interval '6 hours')"):
        # Отметка ДО сбора: канал, который не читается, не долбим каждую минуту.
        await pool.execute("UPDATE va_channel_admin SET last_stats_at=now() WHERE id=$1", a["id"])
        try:
            if await ca.collect_stats(pool, a["owner_id"], a["channel_id"]):
                stats["stats"] += 1
                if a.get("auto_tune"):
                    await ca.autotune(pool, a["owner_id"], a["channel_id"])
        except Exception:
            log.exception("channel_admin_runner: статистика канала %s", a["channel_id"])

    try:
        from services import va_references
        stats["references"] = await va_references.refresh_due(pool)
    except Exception:
        log.exception("channel_admin_runner: каналы-образцы")

    stats["reports"] = await _daily_reports(pool, bot)
    return stats


def _td(**kw):
    from datetime import timedelta
    return timedelta(**kw)


async def _daily_reports(pool, bot) -> int:
    """Раз в сутки — один отчёт владельцу по всем его каналам под администратором."""
    if bot is None:
        return 0
    owners = await pool.fetch(
        "SELECT DISTINCT owner_id FROM va_channel_admin WHERE enabled AND setup_done "
        "AND last_post_at IS NOT NULL "
        "AND (last_report_at IS NULL OR last_report_at < now() - interval '24 hours') LIMIT 20")
    sent = 0
    for o in owners or []:
        owner_id = int(o["owner_id"])
        rows = await pool.fetch(
            "SELECT a.channel_id, a.last_error, "
            "(SELECT MAX(title) FROM managed_channels mc WHERE mc.owner_id=a.owner_id "
            " AND mc.channel_id=a.channel_id) AS title "
            "FROM va_channel_admin a WHERE a.owner_id=$1 AND a.enabled AND a.setup_done",
            owner_id)
        items = []
        for r in rows or []:
            try:
                rep = await ca.channel_report(pool, owner_id, int(r["channel_id"]))
            except Exception:
                log.debug("daily report item failed", exc_info=True)
                continue
            rep["last_error"] = r["last_error"] or ""
            items.append((r["title"] or str(r["channel_id"]), rep))
        if not items:
            # Слать нечего — помечаем на сутки, чтобы не перебирать каждую минуту.
            await _stamp_report(pool, owner_id, retry=False)
            continue
        try:
            await bot.send_message(owner_id, ca.format_daily_report(items), parse_mode="HTML")
            sent += 1
            # Метку ставим ТОЛЬКО после успешной отправки — раньше она
            # обновлялась ДО send_message, и при любом сбое доставки (владелец
            # заблокировал бота, сеть, таймаут) отчёт молча терялся на сутки.
            await _stamp_report(pool, owner_id, retry=False)
        except Exception:
            log.debug("channel_admin_runner: отчёт владельцу %s не ушёл", owner_id, exc_info=True)
            # Не теряем отчёт на сутки: повторим примерно через час.
            await _stamp_report(pool, owner_id, retry=True)
    return sent


async def _stamp_report(pool, owner_id: int, *, retry: bool) -> None:
    """Отметить время суточного отчёта. retry=True → повтор через ~час (не сутки)."""
    # retry сдвигает метку почти на сутки назад: гейт «last_report_at < now()-24h»
    # снова разрешит отчёт примерно через час, а не через сутки, но и не каждую
    # минуту цикла.
    when = "now() - interval '23 hours'" if retry else "now()"
    try:
        await pool.execute(
            f"UPDATE va_channel_admin SET last_report_at={when} "
            "WHERE owner_id=$1 AND enabled",
            owner_id)
    except Exception:
        log.debug("channel_admin_runner: метка отчёта не записана owner=%s", owner_id, exc_info=True)


async def run(pool, bot) -> None:
    log.info("channel_admin_runner: виртуальный администратор каналов запущен")
    while True:
        try:
            await run_once(pool, bot)
        except Exception:
            log.exception("channel_admin_runner: цикл упал, продолжаем")
        await asyncio.sleep(_POLL_INTERVAL_S)
