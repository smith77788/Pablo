from __future__ import annotations

import asyncpg


def _acc_label(acc: asyncpg.Record) -> str:
    name = (acc["first_name"] or "").strip()
    uname = f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
    return f"{name} ({uname})" if name else uname


async def _get_active_accounts(
    pool: asyncpg.Pool, owner_id: int
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                  a.device_model, a.system_version, a.app_version,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.owner_id=$1 AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST, a.added_at""",
        owner_id,
    )


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


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
