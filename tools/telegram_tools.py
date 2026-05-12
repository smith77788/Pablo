"""Telegram Bot tools for BASIC.FOOD AI agents."""
from __future__ import annotations
import os
import httpx
from typing import Any

from tools.database_tools import (
    save_message,
    get_customer_by_telegram,
    upsert_telegram_chat,
)


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _call(method: str, **params: Any) -> dict:
    url = TELEGRAM_API.format(token=_token(), method=method)
    resp = httpx.post(url, json=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_message(chat_id: int | str, text: str, parse_mode: str = "HTML") -> dict:
    """Send a Telegram message and log it."""
    result = _call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)
    customer = get_customer_by_telegram(int(chat_id))
    save_message(
        channel="telegram",
        content=text,
        direction="outbound",
        customer_id=customer["id"] if customer else None,
        chat_id=int(chat_id),
    )
    return result


def send_message_with_keyboard(
    chat_id: int | str,
    text: str,
    buttons: list[list[str]],
) -> dict:
    """Send a message with a reply keyboard."""
    keyboard = {"keyboard": [[{"text": b} for b in row] for row in buttons], "resize_keyboard": True}
    return _call("sendMessage", chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML")


def send_message_with_inline(
    chat_id: int | str,
    text: str,
    inline_buttons: list[list[dict]],
) -> dict:
    """Send a message with inline keyboard. Each button: {text, callback_data}."""
    keyboard = {"inline_keyboard": inline_buttons}
    return _call("sendMessage", chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML")


def get_updates(offset: int = 0, limit: int = 20) -> list[dict]:
    """Poll for new updates (long-poll with timeout=0 for immediate return)."""
    data = _call("getUpdates", offset=offset, limit=limit, timeout=0)
    return data.get("result", [])


def process_update(update: dict) -> dict | None:
    """
    Parse a Telegram update into a unified context dict for the AI agent.
    Looks up or creates customer record, logs the inbound message.
    """
    msg = update.get("message") or update.get("edited_message")
    callback = update.get("callback_query")

    if msg:
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        from_user = msg.get("from", {})
        first_name = from_user.get("first_name", "")
        username = from_user.get("username", "")
    elif callback:
        chat_id = callback["from"]["id"]
        text = callback.get("data", "")
        first_name = callback["from"].get("first_name", "")
        username = callback["from"].get("username", "")
    else:
        return None

    upsert_telegram_chat(chat_id, first_name=first_name, username=username)

    customer = get_customer_by_telegram(chat_id)
    customer_id = customer["id"] if customer else None

    message_id = save_message(
        channel="telegram",
        content=text,
        direction="inbound",
        customer_id=customer_id,
        chat_id=chat_id,
    )

    return {
        "update_id": update["update_id"],
        "message_db_id": message_id,
        "chat_id": chat_id,
        "first_name": first_name,
        "username": username,
        "text": text,
        "is_callback": callback is not None,
        "customer_id": customer_id,
        "customer": customer,
        "is_known_customer": customer is not None,
    }


def answer_callback_query(callback_query_id: str, text: str = "") -> dict:
    return _call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)


def set_webhook(url: str) -> dict:
    return _call("setWebhook", url=url, allowed_updates=["message", "callback_query"])


def delete_webhook() -> dict:
    return _call("deleteWebhook")


def get_bot_info() -> dict:
    return _call("getMe")


def send_photo(chat_id: int | str, photo_url: str, caption: str = "") -> dict:
    return _call("sendPhoto", chat_id=chat_id, photo=photo_url, caption=caption, parse_mode="HTML")


def broadcast(chat_ids: list[int], text: str) -> dict[int, bool]:
    """Send a message to multiple chat IDs. Returns {chat_id: success}."""
    results: dict[int, bool] = {}
    for cid in chat_ids:
        try:
            send_message(cid, text)
            results[cid] = True
        except Exception:
            results[cid] = False
    return results
