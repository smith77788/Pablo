"""📢 Notifications — отправка отчётов в Telegram через бота."""
from __future__ import annotations
import logging
import os
import httpx

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_TELEGRAM_IDS", "")


def _get_admin_ids() -> list[int]:
    if not ADMIN_IDS_RAW:
        return []
    return [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]


def notify(message: str) -> None:
    """Send a message to all admins via Telegram Bot API."""
    if not BOT_TOKEN:
        logger.warning("[notify] TELEGRAM_BOT_TOKEN not set, skipping notification")
        return

    admin_ids = _get_admin_ids()
    if not admin_ids:
        logger.warning("[notify] No ADMIN_TELEGRAM_IDS configured, skipping notification")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in admin_ids:
        try:
            resp = httpx.post(url, json={
                "chat_id": chat_id,
                "text": message[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            if not resp.json().get("ok"):
                logger.warning("[notify] Telegram API error for %d: %s", chat_id, resp.text[:200])
        except Exception as e:
            logger.error("[notify] Failed to send to %d: %s", chat_id, e)
