from __future__ import annotations

import random
import re
import asyncpg
from aiogram.types import CallbackQuery, Message

_FLOOD_RE = re.compile(r"flood.wait|FLOOD_WAIT|FloodWait", re.IGNORECASE)


def backoff(
    attempt: int, base: float = 2.0, cap: float = 120.0, *, jitter: bool = True
) -> float:
    """Exponential backoff: base^attempt capped at cap, with optional +/-20% jitter."""
    raw = min(base**attempt, cap)
    return raw * random.uniform(0.8, 1.2) if jitter else raw


def extract_flood_wait(exc: Exception, err_str: str) -> int:
    """Extract wait seconds from FloodWaitError or an error string.

    Returns 0 when the exception is not a flood wait.
    Supports Telethon FloodWaitError (.seconds) and string formats.
    """
    if hasattr(exc, "seconds"):
        try:
            return int(exc.seconds)
        except (TypeError, ValueError):
            pass
    if not _FLOOD_RE.search(err_str) and "A wait of" not in err_str:
        return 0
    m = re.search(r"(\d+)", err_str)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            pass
    return 60  # fallback when flood wait is detected but seconds are missing


def _acc_label(acc: asyncpg.Record) -> str:
    name = (acc["first_name"] or "").strip()
    uname = f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
    return f"{name} ({uname})" if name else uname


async def _get_active_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    pool_name: str | None = None,
    tags: list[str] | None = None,
) -> list[asyncpg.Record]:
    """Return active accounts, optionally filtered by pool name or tags (ALL tags must match)."""
    conditions = ["a.owner_id=$1", "a.is_active=TRUE", "a.session_str IS NOT NULL"]
    params: list = [owner_id]

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool=${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    where = " AND ".join(conditions)
    return await pool.fetch(
        f"""SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                   a.device_model, a.system_version, a.app_version,
                   a.tags, a.pool, a.labels, a.warnings, a.project,
                   p.proxy_url
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.added_at""",
        *params,
    )


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "#" * filled + "-" * (width - filled)


def _progress_text(title: str, done: int, total: int, ok: int, err: int) -> str:
    pct = round(100 * done / total) if total else 0
    bar = _progress_bar(done, total)
    return (
        f"⏳ <b>{title}</b> {done}/{total}\n"
        f"[{bar}] {pct}%\n"
        f"✅ Успешно: {ok} | ❌ Ошибок: {err}"
    )


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m} мин {s:02d}с" if m else f"{s}с"


async def safe_edit(
    callback: "CallbackQuery",
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
) -> None:
    """Edit message only if content changed, otherwise answer silently."""
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer()
        return
    current = msg.text or msg.caption or ""
    if current == text and msg.reply_markup == reply_markup:
        await callback.answer()
        return
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            await callback.answer()
        else:
            raise
