"""Proxy Manager — manage and check SOCKS5/HTTP proxies.

Entry point: ProxyCb(action="menu")
"""

from __future__ import annotations

import asyncio
import html
import importlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, cast

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ProxyCb, BmCb
from bot.keyboards import subscription_locked_markup
from bot.states import AddProxyFSM
from bot.utils.subscription import require_plan, locked_text
from database import db
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_PROXY_RE = re.compile(
    r"^(socks5|socks4|http)://([^@/]+:[^@/]+@)?[A-Za-z0-9.\-]+:\d+$",
    re.IGNORECASE,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить прокси", callback_data=ProxyCb(action="add"))
    kb.button(text="📋 Мой список", callback_data=ProxyCb(action="list"))
    kb.button(text="✅ Проверить + пинг", callback_data=ProxyCb(action="check_all"))
    kb.button(text="🌍 Определить гео", callback_data=ProxyCb(action="detect_geo"))
    kb.button(text="🆓 Бесплатный пул", callback_data=ProxyCb(action="free_pool"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(2, 2, 1, 1)
    return kb


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ProxyCb(action="menu"))
    return kb


def _cancel_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ProxyCb(action="menu"))
    return kb


async def _check_proxy_alive(proxy_url: str) -> dict:
    """Check proxy reachability via api.telegram.org. Returns {alive, latency_ms}."""
    import time as _time

    try:
        import aiohttp

        socks_module = importlib.import_module("aiohttp_socks")
        ProxyConnector = cast(Any, getattr(socks_module, "ProxyConnector"))

        connector = ProxyConnector.from_url(proxy_url)
        t0 = _time.monotonic()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                "https://api.telegram.org",
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                latency_ms = int((_time.monotonic() - t0) * 1000)
                return {"alive": resp.status < 500, "latency_ms": latency_ms}
    except Exception:
        return {"alive": False, "latency_ms": None}


async def _detect_proxy_geo(proxy_url: str) -> dict:
    """Attempt to detect geo country/city via ip-api.com through the proxy."""
    try:
        import aiohttp

        socks_module = importlib.import_module("aiohttp_socks")
        ProxyConnector = cast(Any, getattr(socks_module, "ProxyConnector"))

        # Extract IP from proxy URL for geo lookup
        import re

        m = re.search(r"[@/](\d+\.\d+\.\d+\.\d+)[:$]", proxy_url)
        if not m:
            return {}
        ip = m.group(1)

        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"http://ip-api.com/json/{ip}?fields=country,city",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "geo_country": data.get("country"),
                        "geo_city": data.get("city"),
                    }
    except Exception:
        log_exc_swallow(log, "Не удалось определить геолокацию прокси")
    return {}


# ── Menu ───────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "menu"))
async def cb_proxy_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🌐 <b>Менеджер прокси</b>\n\n"
        "Управляйте прокси-серверами для аккаунтов и ботов.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── List ───────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "list"))
async def cb_proxy_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    rows = await pool.fetch(
        """SELECT id, label, proxy_url, proxy_type, is_active, last_check, is_alive,
                  latency_avg_ms, geo_country, geo_city, success_rate
           FROM user_proxies
           WHERE owner_id=$1
           ORDER BY COALESCE(success_rate, 100) DESC, created_at DESC""",
        user_id,
    )

    lines = ["📋 <b>Мои прокси</b>\n"]
    kb = InlineKeyboardBuilder()

    if not rows:
        lines.append(
            "Нет добавленных прокси.\n\n"
            "Нажмите <b>➕ Добавить прокси</b>, чтобы добавить первый прокси-сервер.\n"
            "Поддерживаются форматы: <code>socks5://host:port</code>, "
            "<code>socks5://user:pass@host:port</code>, <code>http://host:port</code>."
        )
        kb.button(text="➕ Добавить прокси", callback_data=ProxyCb(action="add"))
    else:
        for row in rows:
            if row["is_alive"] is True:
                status = "✅"
            elif row["is_alive"] is False:
                status = "❌"
            else:
                status = "❓"

            label = row["label"] or row["proxy_url"][:30]
            ptype = row["proxy_type"] or "socks5"
            lat = f" {row['latency_avg_ms']}ms" if row.get("latency_avg_ms") else ""
            geo = f" [{row['geo_country']}]" if row.get("geo_country") else ""
            lines.append(
                f"{status} <code>{html.escape(label)}</code> [{ptype}]{lat}{geo}"
            )
            # Show quality stats from proxy_quality_log (7 days)
            try:
                qstats = await db.get_proxy_quality_stats(pool, row["id"])
                if qstats and qstats.get("total", 0) > 0:
                    s_ok = qstats.get("successes", 0)
                    s_fail = qstats.get("failures", 0)
                    avg_lat = qstats.get("avg_latency")
                    avg_lat_str = f" / ⚡ avg {avg_lat}ms" if avg_lat else ""
                    lines.append(
                        f"   📊 За 7 дней: ✅ {s_ok} успехов / ❌ {s_fail} ошибок{avg_lat_str}"
                    )
            except Exception:
                log_exc_swallow(log, "Не удалось получить статистику качества прокси")
            kb.button(
                text=f"🗑 {html.escape(label[:22])}",
                callback_data=ProxyCb(action="delete", proxy_id=row["id"]),
            )

    kb.button(text="◀️ Назад", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Add — step 1: URL ──────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "add"))
async def cb_proxy_add(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Управление прокси", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    await callback.answer()
    await state.set_state(AddProxyFSM.waiting_url)
    await callback.message.edit_text(
        "🌐 <b>Добавить прокси</b>\n\n"
        "Введите URL прокси в формате:\n"
        "<code>socks5://user:pass@host:port</code>\n"
        "или\n"
        "<code>socks5://host:port</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb().as_markup(),
    )


@router.message(AddProxyFSM.waiting_url)
async def fsm_proxy_url(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    url = (message.text or "").strip()
    if not _PROXY_RE.match(url):
        await message.answer(
            "⚠️ Неверный формат URL.\n"
            "Пример: <code>socks5://user:pass@1.2.3.4:1080</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        return

    await state.update_data(proxy_url=url)
    await state.set_state(AddProxyFSM.waiting_label)

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ProxyCb(action="skip_label"))
    kb.button(text="❌ Отмена", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        "🏷 Введите метку для прокси (например: «Украина 1»)\n"
        "или нажмите <b>Пропустить</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Add — step 2: label ────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "skip_label"))
async def cb_skip_label(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    proxy_url = data.get("proxy_url", "")
    await _save_proxy(
        callback.message, pool, callback.from_user.id, proxy_url, label=None
    )
    await state.clear()


@router.message(AddProxyFSM.waiting_label)
async def fsm_proxy_label(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    label = (message.text or "").strip() or None
    data = await state.get_data()
    proxy_url = data.get("proxy_url", "")
    await _save_proxy(message, pool, message.from_user.id, proxy_url, label=label)
    await state.clear()


async def _save_proxy(
    message: Message,
    pool: asyncpg.Pool,
    owner_id: int,
    proxy_url: str,
    label: str | None,
) -> None:
    # detect type from URL prefix
    proxy_type = "socks5"
    if proxy_url.lower().startswith("http://"):
        proxy_type = "http"
    elif proxy_url.lower().startswith("socks4://"):
        proxy_type = "socks4"

    try:
        await pool.execute(
            """
            INSERT INTO user_proxies (owner_id, label, proxy_url, proxy_type)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (owner_id, proxy_url) DO NOTHING
            """,
            owner_id,
            label,
            proxy_url,
            proxy_type,
        )
        display = label or proxy_url
        text = f"✅ Прокси <code>{display}</code> добавлен."
    except Exception as exc:
        log.exception("Error saving proxy: %s", exc)
        text = f"⚠️ Ошибка сохранения: {exc}"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Список прокси", callback_data=ProxyCb(action="list"))
    kb.button(text="🏠 Меню прокси", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Check all ──────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "check_all"))
async def cb_check_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("Проверяем прокси…")
    user_id = callback.from_user.id

    rows = await pool.fetch(
        "SELECT id, proxy_url, label FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
        user_id,
    )

    if not rows:
        await callback.message.edit_text(
            "📋 Нет активных прокси для проверки.\n\n"
            "Добавьте прокси через <b>➕ Добавить прокси</b>.",
            parse_mode="HTML",
            reply_markup=_menu_kb().as_markup(),
        )
        return

    progress_msg = await callback.message.edit_text(
        f"⏳ Проверяю {len(rows)} прокси...",
        parse_mode="HTML",
    )

    tasks = [_check_proxy_alive(r["proxy_url"]) for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ok_count = 0
    fail_count = 0
    now = datetime.now(timezone.utc)
    lines = ["✅ <b>Проверка прокси завершена</b>\n"]

    async with pool.acquire() as conn:
        for row, result in zip(rows, results):
            if isinstance(result, dict):
                alive = result.get("alive", False)
                latency_ms = result.get("latency_ms")
            else:
                alive = False
                latency_ms = None

            if alive:
                ok_count += 1
                lat_str = f" — {latency_ms}ms" if latency_ms else ""
                lines.append(
                    f"✅ {html.escape(row['label'] or row['proxy_url'][:30])}{lat_str}"
                )
            else:
                fail_count += 1
                lines.append(f"❌ {html.escape(row['label'] or row['proxy_url'][:30])}")

            await conn.execute(
                """UPDATE user_proxies
                   SET is_alive=$1, last_check=$2,
                       latency_avg_ms=CASE WHEN $3::int IS NOT NULL THEN $3::int
                                           ELSE latency_avg_ms END,
                       last_checked_at=$2
                   WHERE id=$4""",
                alive,
                now,
                latency_ms,
                row["id"],
            )
            # Log to proxy_health_log
            try:
                await conn.execute(
                    """INSERT INTO proxy_health_log(proxy_id, owner_id, is_reachable, latency_ms)
                       VALUES($1,$2,$3,$4)""",
                    row["id"],
                    user_id,
                    alive,
                    latency_ms,
                )
            except Exception:
                log_exc_swallow(log, "Не удалось сохранить запись в proxy_health_log")
            # Log to proxy_quality_log (Proxy Intelligence)
            try:
                error_msg = None if alive else "Недоступен"
                await db.log_proxy_quality(
                    pool, row["id"], latency_ms, alive, error_msg
                )
            except Exception:
                log_exc_swallow(log, "Не удалось сохранить запись в proxy_quality_log")

    lines.append(f"\n✅ Рабочих: <b>{ok_count}</b> | ❌ Нерабочих: <b>{fail_count}</b>")
    await progress_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Geo detection ─────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "detect_geo"))
async def cb_detect_geo(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("🌍 Определяю гео прокси...")
    user_id = callback.from_user.id
    rows = await pool.fetch(
        "SELECT id, proxy_url, label FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
        user_id,
    )
    if not rows:
        await callback.message.edit_text(
            "📋 Нет активных прокси.", reply_markup=_menu_kb().as_markup()
        )
        return

    updated = 0
    lines = ["🌍 <b>Гео прокси</b>\n"]
    for row in rows:
        geo = await _detect_proxy_geo(row["proxy_url"])
        label = html.escape(row["label"] or row["proxy_url"][:30])
        if geo:
            country = geo.get("geo_country") or "?"
            city = geo.get("geo_city") or "?"
            lines.append(f"• {label} → {country}, {city}")
            await pool.execute(
                "UPDATE user_proxies SET geo_country=$1, geo_city=$2 WHERE id=$3",
                geo.get("geo_country"),
                geo.get("geo_city"),
                row["id"],
            )
            updated += 1
        else:
            lines.append(f"• {label} → ❓ не определено")

    lines.append(f"\nОпределено: {updated}/{len(rows)}")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_menu_kb().as_markup()
    )


# ── Delete ─────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "delete"))
async def cb_proxy_delete(
    callback: CallbackQuery, callback_data: ProxyCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Управление прокси", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    user_id = callback.from_user.id
    proxy_id = callback_data.proxy_id

    row = await pool.fetchrow(
        "SELECT label, proxy_url FROM user_proxies WHERE id=$1 AND owner_id=$2",
        proxy_id,
        user_id,
    )
    if not row:
        await callback.answer("Прокси не найден.", show_alert=True)
        return
    await callback.answer()

    await pool.execute(
        "DELETE FROM user_proxies WHERE id=$1 AND owner_id=$2",
        proxy_id,
        user_id,
    )

    label = row["label"] or row["proxy_url"]
    await callback.message.edit_text(
        f"🗑 Прокси <code>{label}</code> удалён.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Free proxy pool ─────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "free_pool"))
async def cb_free_pool(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Show free proxy pool stats and trigger manual refresh."""
    await callback.answer()
    from services import proxy_scraper as _ps

    stats = await _ps.get_pool_stats(pool)
    valid = stats["valid"]
    total = stats["total"]
    avg_lat = stats["avg_latency"]
    last_check = stats["last_check"]

    if valid >= 50:
        health_icon = "🟢"
    elif valid >= 20:
        health_icon = "🟡"
    else:
        health_icon = "🔴"

    last_str = last_check.strftime("%d.%m %H:%M") if last_check else "никогда"
    lat_str = f"{avg_lat} мс" if avg_lat else "нет данных"

    text = (
        f"🆓 <b>Бесплатный прокси-пул</b>\n\n"
        f"{health_icon} Валидных прокси: <b>{valid}</b> из {total}\n"
        f"⚡ Средняя задержка: <b>{lat_str}</b>\n"
        f"🕐 Последнее обновление: {last_str}\n\n"
        f"<i>Прокси автоматически применяются к аккаунтам "
        f"без личного прокси и без глобального TG_PROXY. "
        f"Пул обновляется каждые 6 часов из открытых источников.</i>\n\n"
        f"Источники:\n"
        f"• github.com/TheSpeedX/PROXY-List\n"
        f"• github.com/ShiftyTR/Proxy-List\n"
        f"• github.com/monosans/proxy-list\n"
        f"• proxyscrape.com API"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Обновить сейчас", callback_data=ProxyCb(action="free_pool_refresh")
    )
    kb.button(text="◀️ Назад", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ProxyCb.filter(F.action == "free_pool_refresh"))
async def cb_free_pool_refresh(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Trigger manual proxy pool refresh (background task)."""
    await callback.answer("🔄 Запускаю обновление пула...", show_alert=False)
    from services import proxy_scraper as _ps

    progress_msg = await callback.message.edit_text(
        "⏳ <b>Обновление прокси-пула...</b>\n\nЗагружаю и проверяю прокси. Это может занять 1-2 минуты.",
        parse_mode="HTML",
    )

    async def _refresh_bg() -> None:
        try:
            result = await _ps.scrape_and_refresh(pool)
            valid = result.get("valid", 0)
            fetched = result.get("fetched", 0)
            duration = result.get("duration_s", 0)
            icon = "🟢" if valid >= 50 else ("🟡" if valid >= 20 else "🔴")
            text = (
                f"{icon} <b>Пул обновлён!</b>\n\n"
                f"📥 Получено из источников: {fetched}\n"
                f"✅ Прошли проверку: <b>{valid}</b>\n"
                f"⏱ Время: {duration}с"
            )
        except Exception as e:
            text = f"❌ Ошибка обновления: {html.escape(str(e)[:200])}"
        try:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ К пулу", callback_data=ProxyCb(action="free_pool"))
            await progress_msg.edit_text(
                text, parse_mode="HTML", reply_markup=kb.as_markup()
            )
        except Exception:
            log_exc_swallow(log, "_refresh_bg: сбой финального сообщения")

    asyncio.create_task(_refresh_bg())
