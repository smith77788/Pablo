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
from bot.utils.event_status import mark_handled_error
from database import db
from services.logger import log_exc_swallow
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_PROXY_RE = re.compile(
    r"^(socks5|socks4|http)://([^@/]+:[^@/]+@)?[A-Za-z0-9.\-]+:\d+$",
    re.IGNORECASE,
)
_PROXY_PLAN = "pro"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить прокси", callback_data=ProxyCb(action="add"))
    kb.button(text="📥 Массовый импорт", callback_data=ProxyCb(action="add_bulk"))
    kb.button(text="📋 Мой список", callback_data=ProxyCb(action="list"))
    kb.button(text="✅ Проверить + пинг", callback_data=ProxyCb(action="check_all"))
    kb.button(text="🌍 Определить гео", callback_data=ProxyCb(action="detect_geo"))
    kb.button(text="🔍 Проверить уникальность IP", callback_data=ProxyCb(action="check_ip_unique"))
    kb.button(text="🔄 Ротация IP", callback_data=ProxyCb(action="rotate"))
    kb.button(text="🚑 Failover на резерв", callback_data=ProxyCb(action="failover"))
    kb.button(text="🧹 Удалить мёртвые", callback_data=ProxyCb(action="cleanup_dead"))
    kb.button(text="🆓 Бесплатный пул", callback_data=ProxyCb(action="free_pool"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ProxyCb(action="menu"))
    return kb


def _cancel_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ProxyCb(action="menu"))
    return kb


async def _require_proxy_manager(callback: CallbackQuery, pool: asyncpg.Pool) -> bool:
    if await require_plan(pool, callback.from_user.id, _PROXY_PLAN):
        return True
    await safe_answer(callback)
    await callback.message.edit_text(
        locked_text("Управление прокси", _PROXY_PLAN),
        parse_mode="HTML",
        reply_markup=subscription_locked_markup(
            _PROXY_PLAN, back_callback=BmCb(action="monitoring")
        ),
    )
    return False


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
                ssl=False,  # ssl=False намеренно: проверяем ЗАВЕДОМО недоверенный публичный прокси (не боевой API)
            ) as resp:
                latency_ms = int((_time.monotonic() - t0) * 1000)
                return {"alive": resp.status < 500, "latency_ms": latency_ms}
    except Exception:
        return {"alive": False, "latency_ms": None}


async def _detect_proxy_geo(proxy_url: str) -> dict:
    """Attempt to detect geo country/city via ip-api.com through the proxy.

    Works for both auth and no-auth proxy URLs:
      socks5://user:pass@1.2.3.4:1080
      socks5://1.2.3.4:1080
    Uses the proxy's external IP (from ip-api.com) rather than extracting from URL,
    so the geo reflects actual egress location even for hostname-based proxies.
    """
    try:
        import aiohttp

        socks_module = importlib.import_module("aiohttp_socks")
        ProxyConnector = cast(Any, getattr(socks_module, "ProxyConnector"))

        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector) as session:
            # ip-api.com without an IP argument returns geo for the caller's IP,
            # which — routed through the proxy — is the proxy's egress IP.
            async with session.get(
                "http://ip-api.com/json/?fields=country,countryCode,city,query",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Пишем ISO2-код (матч-сайты сравнивают geo_country с ISO2:
                    # UPPER(geo_country)=UPPER('UA')). Раньше писалось полное имя
                    # («Ukraine») → гео-подбор прокси/аккаунтов не срабатывал.
                    from services.geo_normalize import to_iso2

                    return {
                        "geo_country": to_iso2(data.get("countryCode") or data.get("country")),
                        "geo_city": data.get("city"),
                    }
    except Exception:
        log_exc_swallow(log, "Не удалось определить геолокацию прокси")
    return {}


# ── Menu ───────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "menu"))
async def cb_proxy_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "🌐 <b>Менеджер прокси</b>\n\n"
        "Управляйте прокси-серверами для аккаунтов и ботов.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── List ───────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "list"))
async def cb_proxy_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT id, label, proxy_url, proxy_type, is_active, last_check, is_alive,
                      latency_avg_ms, geo_country, geo_city, success_rate,
                      COALESCE(is_backup, FALSE) AS is_backup
               FROM user_proxies
               WHERE owner_id=$1
               ORDER BY COALESCE(success_rate, 100) DESC, created_at DESC""",
            user_id,
        )
    except Exception:
        # Колонка is_backup могла ещё не примениться (лаг миграции) — не роняем
        # весь список, а откатываемся на запрос без неё.
        try:
            rows = await pool.fetch(
                """SELECT id, label, proxy_url, proxy_type, is_active, last_check, is_alive,
                          latency_avg_ms, geo_country, geo_city, success_rate
                   FROM user_proxies
                   WHERE owner_id=$1
                   ORDER BY COALESCE(success_rate, 100) DESC, created_at DESC""",
                user_id,
            )
        except Exception:
            rows = []

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
            geo = ""
            if row.get("geo_country"):
                from services.geo_normalize import flag_emoji
                _fl = flag_emoji(row["geo_country"])
                geo = f" {_fl} [{row['geo_country']}]" if _fl else f" [{row['geo_country']}]"
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
            _is_backup = bool(row["is_backup"]) if "is_backup" in row.keys() else False
            _bk_txt = "🔁 Резерв ✓" if _is_backup else "🔁 В резерв"
            kb.button(
                text=_bk_txt,
                callback_data=ProxyCb(action="toggle_backup", proxy_id=row["id"]),
            )
            kb.button(
                text=f"🗑 {html.escape(label[:18])}",
                callback_data=ProxyCb(action="delete", proxy_id=row["id"]),
            )

    kb.button(text="◀️ Назад", callback_data=ProxyCb(action="menu"))
    kb.adjust(2)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Add — step 1: URL ──────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "add"))
async def cb_proxy_add(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
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
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
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
        # шифруем at-rest; дедуп по детерминированному proxy_fp (шифр недетерминирован)
        from services.token_vault import encrypt_token, proxy_fingerprint

        _fp = proxy_fingerprint(proxy_url)
        _enc = encrypt_token(proxy_url)
        try:
            result = await pool.execute(
                """
                INSERT INTO user_proxies (owner_id, label, proxy_url, proxy_type, proxy_fp)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO NOTHING
                """,
                owner_id, label, _enc, proxy_type, _fp,
            )
        except asyncpg.UndefinedColumnError:
            # proxy_fp ещё не мигрирован (лаг schema_v146) — фолбэк без него.
            result = await pool.execute(
                "INSERT INTO user_proxies (owner_id, label, proxy_url, proxy_type) "
                "VALUES ($1, $2, $3, $4)",
                owner_id, label, _enc, proxy_type,
            )
        display = html.escape(label or proxy_url)
        # "INSERT 0 1" — реально добавлено; "INSERT 0 0" — дубликат (ON CONFLICT).
        # Раньше при дубликате показывался ложный «✅ добавлен».
        inserted = str(result).split()[-1] != "0"
        if inserted:
            text = f"✅ Прокси <code>{display}</code> добавлен."
        else:
            text = (
                f"ℹ️ Прокси <code>{display}</code> уже есть в списке — "
                "повторно не добавлен."
            )
    except Exception as exc:
        log.exception("Error saving proxy: %s", exc)
        text = f"⚠️ Ошибка сохранения: {html.escape(str(exc)[:200])}"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Список прокси", callback_data=ProxyCb(action="list"))
    kb.button(text="🏠 Меню прокси", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Bulk import ──────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "add_bulk"))
async def cb_proxy_add_bulk(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    await state.set_state(AddProxyFSM.waiting_bulk)
    await callback.message.edit_text(
        "📥 <b>Массовый импорт прокси</b>\n\n"
        "Отправьте список прокси, по одному в строке:\n"
        "<code>socks5://user:pass@host:port</code>\n"
        "<code>socks5://host:port|Моя метка</code>\n\n"
        "Метка после <code>|</code> необязательна.",
        parse_mode="HTML",
        reply_markup=_cancel_kb().as_markup(),
    )


@router.message(AddProxyFSM.waiting_bulk)
async def fsm_proxy_bulk(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if not lines:
        await message.answer("⚠️ Пустой список.", reply_markup=_back_kb().as_markup())
        return

    entries: list[tuple[str, str | None]] = []
    invalid = 0
    for line in lines:
        if "|" in line:
            url, label = line.split("|", 1)
            url, label = url.strip(), label.strip() or None
        else:
            url, label = line, None
        if _PROXY_RE.match(url):
            entries.append((url, label))
        else:
            invalid += 1

    if not entries:
        await message.answer(
            f"⚠️ Ни одной валидной строки из {len(lines)}.\n"
            "Формат: <code>socks5://user:pass@host:port</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    from services.token_vault import encrypt_token, proxy_fingerprint

    owner_id = message.from_user.id
    # ОДИН bulk-INSERT вместо N последовательных round-trip'ов (паритет с mini-app
    # import_proxies). Дедуп по детерминированному proxy_fp внутри вставки +
    # ON CONFLICT для уже существующих; шифрование at-rest сохранено.
    seen_fp: set[str] = set()
    encs: list[str] = []
    labels: list[str | None] = []
    ptypes: list[str] = []
    fps: list[str] = []
    intra_dups = 0
    for url, label in entries:
        fp = proxy_fingerprint(url)
        if fp in seen_fp:
            intra_dups += 1
            continue
        seen_fp.add(fp)
        low = url.lower()
        pt = "http" if low.startswith("http://") else "socks4" if low.startswith("socks4://") else "socks5"
        encs.append(encrypt_token(url))
        labels.append(label)
        ptypes.append(pt)
        fps.append(fp)

    added = 0
    if encs:
        try:
            rows = await pool.fetch(
                """INSERT INTO user_proxies (owner_id, label, proxy_url, proxy_type, proxy_fp)
                   SELECT $1, u.lbl, u.enc, u.pt, u.fp
                   FROM unnest($2::text[], $3::text[], $4::text[], $5::text[]) AS u(lbl, enc, pt, fp)
                   ON CONFLICT (owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO NOTHING
                   RETURNING id""",
                owner_id, labels, encs, ptypes, fps,
            )
            added = len(rows)
        except asyncpg.UndefinedColumnError:
            # proxy_fp ещё не мигрирован — фолбэк без него (шифротекст
            # недетерминирован → дубли не отсечёт, но не упадёт).
            rows = await pool.fetch(
                """INSERT INTO user_proxies (owner_id, label, proxy_url, proxy_type)
                   SELECT $1, u.lbl, u.enc, u.pt
                   FROM unnest($2::text[], $3::text[], $4::text[]) AS u(lbl, enc, pt)
                   ON CONFLICT (owner_id, proxy_url) DO NOTHING
                   RETURNING id""",
                owner_id, labels, encs, ptypes,
            )
            added = len(rows)
        except Exception:
            log_exc_swallow(log, "Ошибка массового импорта прокси")
    duplicates = intra_dups + (len(encs) - added)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Список прокси", callback_data=ProxyCb(action="list"))
    kb.button(text="🏠 Меню прокси", callback_data=ProxyCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"📥 <b>Импорт завершён</b>\n\n"
        f"✅ Добавлено: <b>{added}</b>\n"
        + (f"ℹ️ Уже были в списке: <b>{duplicates}</b>\n" if duplicates else "")
        + (f"⚠️ Невалидных строк: <b>{invalid}</b>\n" if invalid else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Check all ──────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "check_all"))
async def cb_check_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await callback.answer("Проверяем прокси…")
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            "SELECT id, proxy_url, label FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
            user_id,
        )
    except Exception:
        rows = []

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
    auto_removed = 0
    now = datetime.now(timezone.utc)
    # Threshold: deactivate proxy after this many consecutive failures
    _DEAD_THRESHOLD = 3
    lines = ["✅ <b>Проверка прокси завершена</b>\n"]

    async with pool.acquire() as conn:
        for row, result in zip(rows, results):
            if isinstance(result, dict):
                alive = result.get("alive", False)
                latency_ms = result.get("latency_ms")
            else:
                alive = False
                latency_ms = None

            label = row["label"] or row["proxy_url"][:30]
            if alive:
                ok_count += 1
                # Classify speed: slow > 3000ms, normal otherwise
                if latency_ms and latency_ms > 3000:
                    lat_str = f" — ⚠️ медленный {latency_ms}ms"
                elif latency_ms:
                    lat_str = f" — {latency_ms}ms"
                else:
                    lat_str = ""
                lines.append(f"✅ {html.escape(label)}{lat_str}")
            else:
                fail_count += 1
                lines.append(f"❌ {html.escape(label)}")

            try:
                # Reset consecutive_failures on success; increment on failure.
                # consecutive_failures column added in schema migration.
                # Use safe fallback: ignore if column missing.
                await conn.execute(
                    """UPDATE user_proxies
                       SET is_alive=$1,
                           last_check=$2,
                           last_checked_at=$2,
                           latency_avg_ms=CASE WHEN $3::int IS NOT NULL
                                               THEN $3::int
                                               ELSE latency_avg_ms END,
                           consecutive_failures=CASE WHEN $1 THEN 0
                                                     ELSE COALESCE(consecutive_failures, 0) + 1 END,
                           is_active=CASE WHEN NOT $1
                                               AND COALESCE(consecutive_failures, 0) + 1 >= $5
                                          THEN FALSE
                                          ELSE is_active END
                       WHERE id=$4""",
                    alive,
                    now,
                    latency_ms,
                    row["id"],
                    _DEAD_THRESHOLD,
                )
                # Check if it was just auto-deactivated
                if not alive:
                    deactivated = await conn.fetchval(
                        "SELECT NOT is_active FROM user_proxies WHERE id=$1",
                        row["id"],
                    )
                    if deactivated:
                        auto_removed += 1
                        lines[-1] += " 🚫 <i>деактивирован (3 провала подряд)</i>"
            except Exception:
                # Fallback without consecutive_failures column (column may not exist yet)
                try:
                    await conn.execute(
                        """UPDATE user_proxies
                           SET is_alive=$1, last_check=$2, last_checked_at=$2,
                               latency_avg_ms=CASE WHEN $3::int IS NOT NULL
                                                   THEN $3::int
                                                   ELSE latency_avg_ms END
                           WHERE id=$4""",
                        alive,
                        now,
                        latency_ms,
                        row["id"],
                    )
                except Exception:
                    log_exc_swallow(log, f"Не удалось обновить прокси id={row['id']}")
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

    summary = f"\n✅ Рабочих: <b>{ok_count}</b> | ❌ Нерабочих: <b>{fail_count}</b>"
    if auto_removed:
        summary += f" | 🚫 Деактивировано: <b>{auto_removed}</b>"
    lines.append(summary)
    await progress_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Geo detection ─────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "detect_geo"))
async def cb_detect_geo(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    await callback.answer("🌍 Определяю гео прокси...")
    user_id = callback.from_user.id
    try:
        rows = await pool.fetch(
            "SELECT id, proxy_url, label FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
            user_id,
        )
    except Exception:
        rows = []
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
            try:
                await pool.execute(
                    "UPDATE user_proxies SET geo_country=$1, geo_city=$2 WHERE id=$3",
                    geo.get("geo_country"),
                    geo.get("geo_city"),
                    row["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, f"Не удалось сохранить гео для прокси id={row['id']}"
                )
            updated += 1
        else:
            lines.append(f"• {label} → ❓ не определено")

    lines.append(f"\nОпределено: {updated}/{len(rows)}")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_menu_kb().as_markup()
    )


# ── IP uniqueness check ───────────────────────────────────────────────────────


def _extract_proxy_host(proxy_url: str) -> str:
    """host[:port] from scheme://[user:pass@]host:port, lowercased for grouping."""
    m = re.match(r"^\w+://(?:[^@/]+@)?([^:/]+):(\d+)$", proxy_url.strip())
    if not m:
        return proxy_url.strip().lower()
    return f"{m.group(1)}:{m.group(2)}".lower()


@router.callback_query(ProxyCb.filter(F.action == "check_ip_unique"))
async def cb_check_ip_unique(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Group the owner's proxies by underlying host:port — two DIFFERENT proxy
    entries sharing the same host mean any accounts assigned to each of them
    are actually indistinguishable by IP, defeating the whole point of
    isolating them onto "separate" proxies (a real risk with shared/free
    proxy providers reusing the same exit IP under different port numbers)."""
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    owner_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            "SELECT id, label, proxy_url FROM user_proxies WHERE owner_id=$1", owner_id
        )
    except Exception:
        rows = []

    if not rows:
        await callback.message.edit_text(
            "🔍 <b>Проверка уникальности IP</b>\n\nНет добавленных прокси.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    from services.token_vault import decrypt_token

    by_host: dict[str, list[dict]] = {}
    for row in rows:
        host = _extract_proxy_host(decrypt_token(row["proxy_url"]))
        by_host.setdefault(host, []).append(dict(row))

    dupes = {h: rs for h, rs in by_host.items() if len(rs) > 1}
    lines = ["🔍 <b>Проверка уникальности IP</b>\n"]
    if not dupes:
        lines.append(f"✅ Все {len(rows)} прокси указывают на разные хосты. Пересечений нет.")
    else:
        lines.append(
            f"⚠️ Найдено {len(dupes)} хост(ов), на которые указывают несколько "
            "разных прокси-записей — аккаунты на них будут выглядеть с одного "
            "и того же IP, даже если формально привязаны к «разным» прокси:\n"
        )
        for host, rs in dupes.items():
            proxy_ids = [r["id"] for r in rs]
            acc_count = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE proxy_id = ANY($1::int[])",
                proxy_ids,
            ) or 0
            labels = ", ".join(html.escape(r["label"] or f"#{r['id']}") for r in rs)
            lines.append(f"• <code>{html.escape(host)}</code>: {labels} — аккаунтов: {acc_count}")

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_menu_kb().as_markup()
    )


# ── Delete ─────────────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "delete"))
async def cb_proxy_delete(
    callback: CallbackQuery, callback_data: ProxyCb, pool: asyncpg.Pool
) -> None:
    if not await _require_proxy_manager(callback, pool):
        return
    user_id = callback.from_user.id
    proxy_id = callback_data.proxy_id

    try:
        row = await pool.fetchrow(
            "SELECT label, proxy_url FROM user_proxies WHERE id=$1 AND owner_id=$2",
            proxy_id,
            user_id,
        )
    except Exception as exc:
        mark_handled_error(f"proxy_delete fetch: {exc}")
        await callback.answer("Ошибка при загрузке прокси.", show_alert=True)
        return
    if not row:
        await callback.answer("Прокси не найден.", show_alert=True)
        return
    await safe_answer(callback)

    try:
        await pool.execute(
            "DELETE FROM user_proxies WHERE id=$1 AND owner_id=$2",
            proxy_id,
            user_id,
        )
    except Exception as exc:
        mark_handled_error(f"proxy_delete execute: {exc}")
        await callback.message.edit_text(
            f"❌ Не удалось удалить прокси: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_menu_kb().as_markup(),
        )
        return

    label = html.escape(row["label"] or row["proxy_url"])
    await callback.message.edit_text(
        f"🗑 Прокси <code>{label}</code> удалён.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Backup toggle / rotation / failover / cleanup (паритет с mini-app) ──────────


@router.callback_query(ProxyCb.filter(F.action == "toggle_backup"))
async def cb_proxy_toggle_backup(
    callback: CallbackQuery, callback_data: ProxyCb, pool: asyncpg.Pool
) -> None:
    """Пометить/снять прокси как РЕЗЕРВНЫЙ (is_backup) — источник для failover."""
    if not await _require_proxy_manager(callback, pool):
        return
    user_id = callback.from_user.id
    proxy_id = callback_data.proxy_id
    try:
        # Self-heal колонки на случай лага миграции.
        await pool.execute(
            "ALTER TABLE user_proxies ADD COLUMN IF NOT EXISTS is_backup BOOLEAN DEFAULT FALSE"
        )
        new_val = await pool.fetchval(
            "UPDATE user_proxies SET is_backup = NOT COALESCE(is_backup, FALSE) "
            "WHERE id=$1 AND owner_id=$2 RETURNING is_backup",
            proxy_id, user_id,
        )
    except Exception as exc:
        mark_handled_error(f"proxy_toggle_backup: {exc}")
        await callback.answer("Ошибка переключения резерва.", show_alert=True)
        return
    if new_val is None:
        await callback.answer("Прокси не найден.", show_alert=True)
        return
    await callback.answer(
        "🔁 Помечен как резервный" if new_val else "Снят из резерва", show_alert=False
    )
    # Перерисовать список с обновлённым состоянием.
    await cb_proxy_list(callback, pool)


@router.callback_query(ProxyCb.filter(F.action == "rotate"))
async def cb_proxy_rotate(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Ротация IP по пулу (anti-detection) — общая реализация с mini-app."""
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    await callback.message.edit_text("🔄 Ротирую назначения прокси…", parse_mode="HTML")
    from services import proxy_rotation
    try:
        res = await proxy_rotation.apply_rotation(pool, callback.from_user.id)
    except Exception as exc:
        mark_handled_error(f"proxy_rotate: {exc}")
        await callback.message.edit_text(
            f"❌ Ошибка ротации: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
        )
        return
    await callback.message.edit_text(
        "🔄 <b>Ротация IP завершена</b>\n\n"
        f"• Переназначено: <b>{res['rotated']}</b>\n"
        f"• Пропущено (заняты операцией): {res['skipped_busy']}\n"
        f"• Не хватило прокси в пуле: {res['skipped_no_proxy']}\n"
        # Мёртвые в пул не берём — иначе ротация переселила бы аккаунты на
        # заведомо нерабочий прокси. Без этой строки «не хватило прокси» при
        # полном списке выглядит беспричинным.
        + (f"• Не взяты (не отвечают): {res.get('skipped_dead', 0)}\n"
           if res.get("skipped_dead") else "")
        + f"• Аккаунтов в группе: {res['accounts']} · Пул: {res['pool']}",
        parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
    )


@router.callback_query(ProxyCb.filter(F.action == "failover"))
async def cb_proxy_failover(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Перевести аккаунты с мёртвым прокси на живой резервный (is_backup)."""
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "🚑 Проверяю прокси и перевожу на резерв…", parse_mode="HTML"
    )
    from services import proxy_selector
    try:
        res = await proxy_selector.failover_dead_proxies(pool, callback.from_user.id)
    except Exception as exc:
        mark_handled_error(f"proxy_failover: {exc}")
        await callback.message.edit_text(
            f"❌ Ошибка failover: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
        )
        return
    reassigned = res.get("reassigned") or []
    no_backup = res.get("still_dead_no_backup") or []
    await callback.message.edit_text(
        "🚑 <b>Failover завершён</b>\n\n"
        f"• Проверено назначенных прокси: <b>{res.get('checked', 0)}</b>\n"
        f"• Живых: {res.get('healthy', 0)}\n"
        f"• Переведено на резерв: <b>{len(reassigned)}</b>\n"
        f"• Мёртвых без резерва: {len(no_backup)}\n"
        f"• Резервных доступно: {res.get('backups_available', 0)} "
        f"(живых {res.get('backups_healthy', 0)})",
        parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
    )


@router.callback_query(ProxyCb.filter(F.action == "cleanup_dead"))
async def cb_proxy_cleanup_dead(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Удалить подтверждённо-мёртвые НЕназначенные прокси (изоляция сохранена)."""
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
    user_id = callback.from_user.id
    try:
        removed = await pool.fetch(
            """DELETE FROM user_proxies up
               WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                 AND NOT EXISTS (
                     SELECT 1 FROM tg_accounts a
                     WHERE a.owner_id=$1 AND a.proxy_id=up.id)
               RETURNING id""",
            user_id,
        )
        skipped = await pool.fetchval(
            """SELECT COUNT(*) FROM user_proxies up
               WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                 AND EXISTS (SELECT 1 FROM tg_accounts a
                             WHERE a.owner_id=$1 AND a.proxy_id=up.id)""",
            user_id,
        ) or 0
    except Exception as exc:
        mark_handled_error(f"proxy_cleanup_dead: {exc}")
        await callback.message.edit_text(
            f"❌ Ошибка очистки: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
        )
        return
    await callback.message.edit_text(
        "🧹 <b>Очистка мёртвых прокси</b>\n\n"
        f"• Удалено: <b>{len(removed)}</b>\n"
        f"• Пропущено (назначены аккаунтам): {int(skipped)}\n\n"
        "<i>Назначенные мёртвые не удаляются, чтобы не потерять изоляцию — "
        "сначала переведите их через 🚑 Failover.</i>",
        parse_mode="HTML", reply_markup=_menu_kb().as_markup(),
    )


# ── Free proxy pool ─────────────────────────────────────────────────────────────


@router.callback_query(ProxyCb.filter(F.action == "free_pool"))
async def cb_free_pool(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Show free proxy pool stats and trigger manual refresh."""
    if not await _require_proxy_manager(callback, pool):
        return
    await safe_answer(callback)
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
    if not await _require_proxy_manager(callback, pool):
        return
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
            validated = result.get("validated", fetched)
            duration = result.get("duration_s", 0)
            icon = "🟢" if valid >= 50 else ("🟡" if valid >= 20 else "🔴")
            text = (
                f"{icon} <b>Пул обновлён!</b>\n\n"
                f"📥 Получено из источников: {fetched}\n"
                f"🔎 Проверено: {validated}\n"
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

    from services.bg_tasks import spawn  # strong-ссылка (класс #14)
    spawn(_refresh_bg())
