"""Вход мини-аппа: initData с датой из будущего и без пользователя — отказ.

Было: подпись проверялась, но auth_date из будущего проходил (строка жила
сутки СВЕРХ положенного), а initData без поля user давал токен на id 0.
Тест падает без фикса.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

from services.mini_app_auth import validate_init_data

TOKEN = "123456789:AAEhBP0av28bGtUcLzZ2Dz5UudzRo7PkLg0"


def _sign(fields: dict) -> str:
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": h})


def _user(uid=42):
    return json.dumps({"id": uid, "first_name": "A"})


def test_valid_init_data_passes():
    r = validate_init_data(_sign({"auth_date": str(int(time.time())), "user": _user()}), TOKEN)
    assert r and r["user_id"] == 42


def test_future_auth_date_rejected():
    future = str(int(time.time()) + 3600)
    assert validate_init_data(_sign({"auth_date": future, "user": _user()}), TOKEN) is None


def test_small_clock_skew_tolerated():
    near = str(int(time.time()) + 30)
    assert validate_init_data(_sign({"auth_date": near, "user": _user()}), TOKEN)


def test_missing_or_bad_user_rejected():
    now = str(int(time.time()))
    assert validate_init_data(_sign({"auth_date": now}), TOKEN) is None
    assert validate_init_data(_sign({"auth_date": now, "user": _user(0)}), TOKEN) is None
    assert validate_init_data(_sign({"auth_date": now, "user": _user(-5)}), TOKEN) is None
    assert validate_init_data(_sign({"auth_date": now, "user": "[1]"}), TOKEN) is None


def test_tampered_hash_rejected():
    s = _sign({"auth_date": str(int(time.time())), "user": _user()})
    assert validate_init_data(s.replace("42", "43"), TOKEN) is None
