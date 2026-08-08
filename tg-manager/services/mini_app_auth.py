"""Telegram Mini App — initData validation and session tokens."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Optional


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Validate Telegram Mini App initData using HMAC-SHA256.
    Returns user dict on success, None on failure.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_value = parsed.pop("hash", None)
        if not hash_value:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_value):
            return None
        auth_date = int(parsed.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return {
            "user_id": int(user.get("id", 0)),
            "username": user.get("username", ""),
            "first_name": user.get("first_name", ""),
        }
    except Exception:
        return None


def make_token(user_id: int, bot_token: str) -> str:
    """Generate a 2-hour session token."""
    ts = int(time.time())
    payload = f"{user_id}:{ts}"
    secret = hashlib.sha256(bot_token.encode()).digest()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def parse_token(token: str, bot_token: str, max_age: int = 7200) -> Optional[int]:
    """Validate a session token, return user_id or None."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        uid, ts_s, sig = parts
        if time.time() - int(ts_s) > max_age:
            return None
        payload = f"{uid}:{ts_s}"
        secret = hashlib.sha256(bot_token.encode()).digest()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(expected, sig):
            return None
        return int(uid)
    except Exception:
        return None
