"""Секретное слово владельца против фишинга (волна 4).

Фразу, которую знает только владелец, показывает КАЖДАЯ наша тревога —
подделку «от Infragram» без неё владелец узнаёт сразу. По желанию.
"""
from __future__ import annotations

import os

from services import owner_safeword as sw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_phrase_length_and_trim():
    assert sw.normalize_phrase("  кит  ") == "кит"
    assert sw.normalize_phrase("синий   слон") == "синий слон", "пробелы схлопываются"
    assert sw.normalize_phrase("a") is None, "один символ — мало"
    assert sw.normalize_phrase("x" * 49) is None, "длиннее 48 — нет"
    assert sw.normalize_phrase("") is None
    assert sw.normalize_phrase(None) is None


def test_suffix_is_appended_and_escaped():
    s = sw.alert_suffix("синий кит")
    assert "Ваше слово" in s and "синий кит" in s
    assert "подделка" in s
    assert s.startswith("\n\n"), "хвост отделён от тела тревоги"
    assert sw.alert_suffix("") == "", "нет слова — нет хвоста"
    assert sw.alert_suffix(None) == ""
    assert "<b>" not in sw.alert_suffix("<b>x</b>")[9:] or "&lt;b&gt;" in sw.alert_suffix("<b>x</b>")


def test_every_owner_alert_carries_the_word():
    # Вход владельца.
    osess = _src("services/owner_sessions.py")
    assert "owner_safeword.line(pool, owner_id)" in osess
    # Тревоги безопасности аккаунта.
    alarm = _src("services/account_alarm.py")
    assert "owner_safeword.line(pool, owner)" in alarm
    # Сигнал о сливе данных.
    guard = _src("services/owner_guard.py")
    assert "owner_safeword.line(pool, int(owner_id))" in guard


def test_routes_and_frontend_are_wired():
    api = _src("services/mini_app_api.py")
    assert '/api/miniapp/owner/safeword", owner_safeword_get' in api
    assert '/api/miniapp/owner/safeword", owner_safeword_set' in api
    assert '"safeword_suggested": await _owner_safeword.suggested(pool, uid)' in api
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "POST /api/miniapp/owner/safeword" in snap

    html = _src("mini_app/index.html")
    assert "function ownSafewordHtml" in html and "ownSafewordSet" in html
    assert "shieldSafewordNudge" in html and "safeword_suggested" in html
