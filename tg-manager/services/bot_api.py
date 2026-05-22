"""Async Telegram Bot API wrapper for managed (target) bots."""
from __future__ import annotations
import asyncio
import aiohttp
from config import MAX_CONCURRENT

_semaphore: asyncio.Semaphore | None = None

TG = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{file_path}"


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


async def _call(session: aiohttp.ClientSession, token: str, method: str,
                **params) -> dict:
    url = TG.format(token=token, method=method)
    payload = {k: v for k, v in params.items() if v is not None}
    async with _sem():
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()


# ── Bot info ──────────────────────────────────────────────────────────────

async def get_me(session: aiohttp.ClientSession, token: str) -> dict | None:
    data = await _call(session, token, "getMe")
    return data.get("result") if data.get("ok") else None


# ── Profile editing ───────────────────────────────────────────────────────

async def set_name(session: aiohttp.ClientSession, token: str, name: str,
                   language_code: str = "") -> bool:
    data = await _call(session, token, "setMyName",
                       name=name, language_code=language_code or None)
    return data.get("ok", False)


async def set_description(session: aiohttp.ClientSession, token: str, description: str,
                           language_code: str = "") -> bool:
    data = await _call(session, token, "setMyDescription",
                       description=description, language_code=language_code or None)
    return data.get("ok", False)


async def set_short_description(session: aiohttp.ClientSession, token: str,
                                 short_description: str, language_code: str = "") -> bool:
    data = await _call(session, token, "setMyShortDescription",
                       short_description=short_description,
                       language_code=language_code or None)
    return data.get("ok", False)


async def set_photo(session: aiohttp.ClientSession, token: str,
                    photo_bytes: bytes, filename: str = "photo.jpg") -> bool:
    """Upload raw photo bytes to the managed bot via multipart form."""
    url = TG.format(token=token, method="setMyPhoto")
    form = aiohttp.FormData()
    form.add_field("photo", photo_bytes, filename=filename, content_type="image/jpeg")
    async with _sem():
        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
    return data.get("ok", False)


async def delete_my_photo(session: aiohttp.ClientSession, token: str) -> bool:
    data = await _call(session, token, "deleteMyPhoto")
    return data.get("ok", False)


# ── Webhooks ──────────────────────────────────────────────────────────────

async def set_webhook(session: aiohttp.ClientSession, token: str, url: str) -> dict:
    return await _call(session, token, "setWebhook", url=url,
                       allowed_updates=["message", "callback_query", "chat_member"])


async def delete_webhook(session: aiohttp.ClientSession, token: str) -> dict:
    return await _call(session, token, "deleteWebhook")


async def get_webhook_info(session: aiohttp.ClientSession, token: str) -> dict:
    data = await _call(session, token, "getWebhookInfo")
    return data.get("result", {}) if data.get("ok") else {}


# ── Audience collection ───────────────────────────────────────────────────

async def fetch_updates(session: aiohttp.ClientSession, token: str) -> list[dict]:
    """Pull up to 100 pending updates (non-destructive offset=-1 not possible;
    this DOES consume updates — acceptable for bots managed exclusively here)."""
    data = await _call(session, token, "getUpdates", offset=0, limit=100, timeout=0)
    return data.get("result", []) if data.get("ok") else []


def extract_users_from_updates(updates: list[dict]) -> list[dict]:
    """Parse unique users from a batch of Telegram updates."""
    seen: set[int] = set()
    users: list[dict] = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or upd.get("callback_query")
        if not msg:
            continue
        from_user = msg.get("from") or {}
        uid = from_user.get("id")
        if not uid or uid in seen or from_user.get("is_bot"):
            continue
        seen.add(uid)
        users.append({
            "user_id": uid,
            "username": from_user.get("username"),
            "first_name": from_user.get("first_name"),
            "last_name": from_user.get("last_name"),
            "language_code": from_user.get("language_code"),
        })
    return users


# ── Sending ───────────────────────────────────────────────────────────────

async def send_message(session: aiohttp.ClientSession, token: str,
                        chat_id: int, text: str) -> tuple[bool, int | None]:
    """Returns (success, retry_after_seconds_or_None)."""
    data = await _call(session, token, "sendMessage",
                       chat_id=chat_id, text=text, parse_mode="HTML")
    if data.get("ok"):
        return True, None
    error_code = data.get("error_code", 0)
    if error_code == 429:
        retry = data.get("parameters", {}).get("retry_after", 5)
        return False, retry
    return False, None


# ── Batch operations ──────────────────────────────────────────────────────

async def batch_get_me(session: aiohttp.ClientSession,
                        tokens: list[str]) -> dict[str, dict | None]:
    """Call getMe on many bots concurrently. Returns {token: result}."""
    results = await asyncio.gather(
        *(get_me(session, t) for t in tokens), return_exceptions=True
    )
    return {
        token: (r if not isinstance(r, Exception) else None)
        for token, r in zip(tokens, results)
    }
