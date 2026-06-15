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
    action_type: str = "default",
    respect_cooldown: bool = True,
) -> list[asyncpg.Record]:
    """Return active accounts through the central resource selector."""
    from services import resource_selector

    return await resource_selector.select_all_active(
        pool,
        owner_id,
        pool_name=pool_name,
        tags=tags,
        action_type=action_type,
        respect_cooldown=respect_cooldown,
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
    """Edit message. Dismisses the button spinner immediately, then updates content."""
    # Dismiss spinner right away — user gets instant feedback
    try:
        await callback.answer()
    except Exception:
        pass

    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return
        if "there is no text in the message to edit" in err:
            try:
                await msg.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                pass
        elif "message to edit not found" in err or "message can't be edited" in err:
            try:
                await callback.bot.send_message(
                    callback.from_user.id, text, parse_mode=parse_mode, reply_markup=reply_markup
                )
            except Exception:
                pass
