"""Бот-зеркало: личный запасной вход в панель через собственный бот владельца.

Просьба владельца (17.11.2026): каждый должен уметь быстро создать своего бота с
нашим проектом (зеркало), чтобы пользоваться панелью, если с основным ботом
что-то случится.

Ключ безопасности: мини-апп, открытый из зеркала, подписан ТОКЕНОМ ЗЕРКАЛА —
/auth проверяет initData этим токеном, но сессию выдаёт ОСНОВНЫМ токеном, поэтому
остальные ручки (их сотни) не меняются.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import pytest

from services import mirror_bot as mb
from services.mini_app_auth import make_token, parse_token, validate_init_data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _make_init_data(token: str, user_id: int = 555) -> str:
    """Собрать валидный Telegram initData, подписанный данным токеном."""
    user = json.dumps({"id": user_id, "username": "owner", "first_name": "O"})
    fields = {"auth_date": str(int(time.time())), "user": user, "query_id": "AA"}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    fields["hash"] = h
    return urllib.parse.urlencode(fields)


MAIN = "100200:MAINtokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MIRROR = "900800:MIRRORtokenBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def test_mirror_initdata_validates_only_with_mirror_token():
    init = _make_init_data(MIRROR, user_id=555)
    # Зеркальный токен подпись подтверждает…
    user = validate_init_data(init, MIRROR)
    assert user and user["user_id"] == 555
    # …а основной — НЕТ (иначе проверка зеркала была бы бессмысленной).
    assert validate_init_data(init, MAIN) is None


def test_session_token_is_always_main_token():
    # После входа через зеркало сессия обязана проверяться ОСНОВНЫМ токеном —
    # его знает _get_uid на каждом запросе.
    sess = make_token(555, MAIN)
    assert parse_token(sess, MAIN) == 555
    assert parse_token(sess, MIRROR) is None


def test_menu_url_carries_mirror_id(monkeypatch):
    monkeypatch.setattr(mb, "MINI_APP_URL", "https://app.example.com/")
    assert mb.menu_url(7) == "https://app.example.com?m=7"
    monkeypatch.setattr(mb, "MINI_APP_URL", "https://app.example.com/?v=2")
    assert mb.menu_url(7) == "https://app.example.com/?v=2&m=7"


def test_register_rejects_malformed_token():
    async def _getme(http, token):  # не должно дойти
        raise AssertionError("getMe не должен вызываться на явном мусоре")
    with pytest.raises(mb.MirrorError):
        asyncio.run(mb.register(object(), 7, "не-токен", object()))


def test_register_rejects_main_bot_token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", MAIN)

    async def _getme(http, token):
        return {"id": int(MAIN.split(":")[0]), "username": "mainbot", "is_bot": True}
    monkeypatch.setattr(mb.bot_api, "get_me", _getme)
    with pytest.raises(mb.MirrorError):
        asyncio.run(mb.register(object(), 7, MAIN, object()))


def test_auth_handler_validates_against_mirror_then_issues_main_session():
    # Контракт в коде: /auth берёт токен зеркала по body['mirror'] для ПРОВЕРКИ,
    # а make_token выдаёт сессию на основном bot_token.
    src = _src("services/mini_app_api.py")
    a = src[src.index("async def auth("):]
    a = a[:a.index("async def mirror_get(")]
    assert "validate_mirror_user(pool, _mid, init_data, validate_init_data)" in a
    assert 'login_kind = "mirror"' in a
    assert "validate_init_data(init_data, bot_token)" in a
    assert "make_token(user[\"user_id\"], bot_token, sid=sid)" in a, "сессия должна быть на основном токене"


def test_routes_registered():
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "GET /api/miniapp/mirror" in snap
    assert "POST /api/miniapp/mirror" in snap
    assert "DELETE /api/miniapp/mirror" in snap


def test_frontend_passes_mirror_id_and_has_screen():
    html = _src("mini_app/index.html")
    assert "function _mirrorId(" in html and "_body.mirror" in html
    assert "async function mirrorConnect(" in html and "async function loadMirror(" in html
    assert "/api/miniapp/mirror" in html
