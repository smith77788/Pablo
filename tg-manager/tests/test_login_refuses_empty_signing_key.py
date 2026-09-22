"""Пустой BOT_TOKEN не должен превращаться в общеизвестный ключ подписи.

Вход в мини-апп подписывается ключом, выведенным из токена бота:
sha256(BOT_TOKEN). Если переменной нет, os.getenv возвращает "" — и ключом
становится sha256(b""), константа, которую знает кто угодно. Дальше это не
«вход сломался», а «входа нет»: злоумышленник сам подписывает сессионный
токен с ЛЮБЫМ user_id и работает от имени любого владельца — его аккаунты,
строки сессий, операции, платежи. Снаружи всё выглядит рабочим: мини-апп
открывается, вход «проходит».

Пустой токен — не теория: веб-роль (INFRAGRAM_ROLE=web) поднимается отдельным
процессом, и переменную легко не донести до неё при редеплое или
переименовании.

Тест подделывает токены ровно так, как их подписал бы старый код с пустым
секретом, и требует отказа. На старом коде подделки принимались.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import time

import pytest

from services import device_pairing as D
from services.mini_app_auth import (
    make_token,
    parse_token,
    signing_secret_ok,
    validate_init_data,
)

_EMPTY_KEYS = ("", "   ", None)


def _forged_session_token(uid: int, secret: str = "") -> str:
    """Сессионный токен, подписанный ключом из пустого (или чужого) секрета."""
    ts = int(time.time())
    payload = f"{uid}:{ts}"
    key = hashlib.sha256(secret.encode()).digest()
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def _forged_init_data(uid: int, secret: str = "") -> str:
    """initData, подписанная по схеме Telegram ключом из пустого секрета."""
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": uid, "username": "attacker", "first_name": "A"}),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", secret.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, data_check.encode(), hashlib.sha256).hexdigest()
    import urllib.parse

    return urllib.parse.urlencode(fields)


def _forged_device_token(uid: int, secret: str = "") -> str:
    ts = int(time.time())
    payload = f"{uid}:{ts}:deadbeefdeadbeef"
    key = hashlib.sha256(secret.encode()).digest()
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"dev:{payload}:{sig}"


# ── Ключ признаётся негодным ────────────────────────────────────────────────


@pytest.mark.parametrize("bad", _EMPTY_KEYS)
def test_empty_secret_is_rejected(bad):
    assert signing_secret_ok(bad) is False


def test_obviously_not_a_token_is_rejected():
    assert signing_secret_ok("test") is False, (
        "короткая строка-заглушка не должна сходить за токен бота"
    )


def test_real_looking_token_is_accepted():
    assert signing_secret_ok("1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw") is True


# ── Сессионный токен мини-аппа ──────────────────────────────────────────────


def test_forged_session_token_is_refused():
    forged = _forged_session_token(999)
    assert parse_token(forged, "") is None, (
        "сессия, подписанная ключом из пустого BOT_TOKEN, принята — "
        "вход подделывается от имени любого владельца"
    )


def test_session_token_is_not_issued_without_key():
    with pytest.raises(ValueError):
        make_token(999, "")


# ── initData от Telegram ────────────────────────────────────────────────────


def test_forged_init_data_is_refused():
    assert validate_init_data(_forged_init_data(999), "") is None, (
        "поддельная initData принята при пустом BOT_TOKEN"
    )


# ── Токен устройства (живёт месяц) ──────────────────────────────────────────


def test_forged_device_token_is_refused():
    assert D.parse_device_token(_forged_device_token(999), "") is None, (
        "поддельный токен устройства принят — вход от имени любого владельца "
        "на весь срок жизни токена"
    )


def test_device_token_is_not_issued_without_key():
    with pytest.raises(ValueError):
        D.make_device_token(999, "")


# ── Двери снаружи: публичные ручки входа обязаны отказывать раньше работы ───


def _handler(name: str) -> ast.AsyncFunctionDef:
    src = open(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "services",
            "mini_app_api.py",
        ),
        encoding="utf-8",
    ).read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"ручка {name} не найдена — тест устарел")


@pytest.mark.parametrize("name", ["auth", "pair_device", "pair_exchange"])
def test_public_login_endpoints_check_the_key(name):
    fn = _handler(name)
    calls = [
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "signing_secret_ok" in calls, (
        f"ручка {name} не проверяет ключ подписи: при пустом BOT_TOKEN она "
        "либо примет подделку, либо упадёт пятисоткой вместо понятного отказа"
    )
