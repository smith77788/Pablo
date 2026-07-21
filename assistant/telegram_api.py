"""Thin Telegram Bot API client for the personal assistant bot.

Uses its own token (ASSISTANT_BOT_TOKEN) so it never collides with the
customer-facing bot token in TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

API_URL = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"

# Telegram hard limit is 4096 chars; keep headroom for the part counter.
MAX_MESSAGE_LEN = 4000


def _token() -> str:
    token = os.environ.get("ASSISTANT_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "ASSISTANT_BOT_TOKEN не задан. Добавьте токен бота в переменные окружения."
        )
    return token


def call(method: str, timeout: float = 35.0, _retries: int = 3, **params: Any) -> Any:
    """Call a Bot API method and return its `result` payload.

    Retries transient network failures and honours Telegram's flood-control
    `retry_after` so one hiccup never kills the polling loop.
    """
    url = API_URL.format(token=_token(), method=method)
    last_exc: Exception | None = None
    for attempt in range(_retries):
        try:
            resp = httpx.post(url, json=params, timeout=timeout)
            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(
                        resp.json().get("parameters", {}).get("retry_after", 5)
                    )
                except Exception:
                    pass
                time.sleep(min(retry_after, 60))
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API {method} failed: {data}")
            return data.get("result")
        except httpx.TransportError as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
    raise last_exc or RuntimeError(f"Telegram API {method}: retries exhausted")


def get_me() -> dict:
    return call("getMe")


def reset_old_connections() -> None:
    """Detach whatever was previously wired to this bot token.

    Removes any webhook another service registered and drops updates queued
    while the old integration was running, so the new polling loop starts
    from a clean slate.
    """
    call("deleteWebhook", drop_pending_updates=True)


def get_updates(offset: int = 0) -> list[dict]:
    """Long-poll for updates; blocks up to 25 s server-side."""
    return call(
        "getUpdates",
        offset=offset,
        timeout=25,
        allowed_updates=["message"],
    )


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    try:
        call("sendChatAction", chat_id=chat_id, action=action)
    except Exception:
        pass  # cosmetic only — never fail a turn over it


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split text into Telegram-sized chunks, preferring newline boundaries."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = text.rfind(" ", limit // 2, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def send_message(chat_id: int, text: str) -> None:
    """Send text as plain messages, splitting anything over the 4096 limit."""
    parts = split_message(text)
    if not parts:
        parts = ["(пустой ответ)"]
    total = len(parts)
    for i, part in enumerate(parts, 1):
        prefix = f"[{i}/{total}]\n" if total > 1 else ""
        call("sendMessage", chat_id=chat_id, text=prefix + part)
        if total > 1:
            time.sleep(0.1)


def download_file(file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
    """Download a Telegram file. Returns (content, file_path)."""
    info = call("getFile", file_id=file_id)
    path = info["file_path"]
    size = info.get("file_size") or 0
    if size > max_bytes:
        raise ValueError(f"Файл слишком большой: {size} байт (лимит {max_bytes})")
    url = FILE_URL.format(token=_token(), path=path)
    resp = httpx.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content, path
