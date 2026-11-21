"""Входы владельца в Infragram видны, завершаются, а о новом устройстве — тревога.

Просьба владельца 21.11.2026: защитить и аккаунт, который управляет
Infragram. До этого вход владельца нигде не записывался, а самоподписанный
токен жил свои два часа: чужой вход нельзя было ни увидеть, ни завершить.
"""
from __future__ import annotations

import asyncio
import os

from services import mini_app_auth as auth
from services import owner_sessions as osess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "1234567890:AAtesttesttesttesttesttesttesttestt"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148 Telegram-iOS/11.2")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36 Telegram-Android/11.2")


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Токен с номером сеанса ───────────────────────────────────────────────────

def test_token_carries_the_session_and_old_tokens_still_work():
    tok = auth.make_token(42, KEY, sid="abc_DEF-1")
    assert auth.parse_token_sid(tok, KEY) == (42, "abc_DEF-1")
    assert auth.parse_token(tok, KEY) == 42
    old = auth.make_token(42, KEY)
    assert auth.parse_token_sid(old, KEY) == (42, None), "токены до выкатки не должны ломаться"


def test_session_number_cannot_be_swapped():
    tok = auth.make_token(42, KEY, sid="mine")
    uid, ts, _sid, sig = tok.split(":")
    forged = f"{uid}:{ts}:other:{sig}"
    assert auth.parse_token(forged, KEY) is None, "номер сеанса не подписан"
    # И нельзя выкинуть номер, чтобы уйти от отзыва.
    assert auth.parse_token(f"{uid}:{ts}:{sig}", KEY) is None


def test_revoked_session_is_refused_on_every_request(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api as api
    monkeypatch.setattr(api, "_bot_token", lambda: KEY)
    tok = auth.make_token(42, KEY, sid="s1")
    req = make_mocked_request("GET", "/api/miniapp/accounts",
                              headers={"Authorization": "Bearer " + tok})
    osess._REVOKED.discard("s1")
    assert api._get_uid(req) == 42
    osess._REVOKED.add("s1")
    try:
        assert api._get_uid(req) is None, "завершённый вход по-прежнему открывает панель"
    finally:
        osess._REVOKED.discard("s1")


# ── Устройство ───────────────────────────────────────────────────────────────

def test_device_label_is_human():
    assert osess.device_label(IPHONE) == "iPhone · iOS 17.5 · Telegram"
    assert osess.device_label(ANDROID) == "SM-S918B · Android 14 · Telegram"
    assert osess.device_label("") == "Неизвестное устройство"


def test_new_device_and_new_country_alarm_but_first_login_does_not():
    fp_i, fp_a = osess.device_fp(IPHONE), osess.device_fp(ANDROID)
    hist = [{"fp": fp_i, "country": "UA"}]
    assert osess.is_alert_worthy([], {"fp": fp_a, "country": "NL"}) is None, (
        "первый вход после выкатки — не тревога, иначе каждый владелец получит её о себе")
    assert osess.is_alert_worthy(hist, {"fp": fp_a, "country": "UA"}) == "device"
    assert osess.is_alert_worthy(hist, {"fp": fp_i, "country": "NL"}) == "country"
    assert osess.is_alert_worthy(hist, {"fp": fp_i, "country": "UA"}) is None
    assert osess.is_alert_worthy(hist, {"fp": fp_i, "country": ""}) is None


def test_alert_says_what_to_do_in_telegram():
    text = osess.build_alert({"label": "Windows · Chrome", "ip": "203.0.113.9", "country": "NL"},
                             "telegram", "device")
    assert "нового устройства" in text and "Настройки → Устройства" in text
    assert "<script>" not in osess.build_alert({"label": "<script>"}, "telegram", "device")


# ── Проводка ─────────────────────────────────────────────────────────────────

def test_every_login_path_records_the_session():
    api = _src("services/mini_app_api.py")
    a = api[api.index("async def auth("):api.index("async def mirror_get(")]
    assert "_os.open_session(" in a and "make_token(user[\"user_id\"], bot_token, sid=sid)" in a
    p = api[api.index("async def pair_exchange("):api.index("async def devices_list(")]
    assert "_os.open_session(" in p and "pairing_id=pairing_id" in p and "sid=sid" in p
    # Отзыв ключа устройства завершает и его живой сеанс.
    r = api[api.index("async def devices_revoke("):]
    assert "revoke_by_pairing(" in r[:1500]


def test_revocations_are_synced_in_every_role():
    main = _src("main.py")
    assert '_web_resilient("owner_session_revocations", run_revocation_sync, pool)' in main


def test_screen_and_bot_can_end_a_login():
    html = _src("mini_app/index.html")
    assert "/api/miniapp/owner/sessions" in html and "function ownSessRevokeOthers" in html
    bot = _src("bot/handlers/account_shield_hub.py")
    assert "OwnSessCb.filter(" in bot and "owner_sessions.revoke(" in bot


def test_sync_drops_expired_and_adds_new():
    class P:
        async def fetch(self, sql, *a):
            return [{"sid": "new"}]
    osess._REVOKED.add("stale")
    try:
        asyncio.run(osess.sync_revoked(P()))
        assert osess.is_revoked("new") and not osess.is_revoked("stale")
    finally:
        osess._REVOKED.discard("new")
