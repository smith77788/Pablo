"""Код Infragram владельца — вторая стена перед сливом данных (волна 3).

Код по желанию (решение владельца продукта 21.11.2026): пока не поставлен,
ничего не меняется. Когда поставлен — спрашивается перед опасным действием
(экспорт сессии, показ паролей 2FA, удаление). А сигнал о таком действии
уходит владельцу в любом случае, с кодом и без.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from services import owner_guard as og

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Код: нормализация и хеш ──────────────────────────────────────────────────

def test_pin_must_be_four_to_eight_digits():
    assert og.normalize_pin("1234") == "1234"
    assert og.normalize_pin(" 12 34-56 ") == "123456"
    assert og.normalize_pin("123") is None, "три цифры — мало"
    assert og.normalize_pin("123456789") is None, "девять — много"
    assert og.normalize_pin("12ab") is None
    assert og.normalize_pin("") is None
    assert og.normalize_pin(None) is None


def test_hash_is_salted_and_verifies_in_constant_time():
    h1 = og.hash_pin("4821")
    h2 = og.hash_pin("4821")
    assert h1 != h2, "соль своя у каждого хеша"
    assert h1.startswith("scrypt$")
    assert og.verify_pin("4821", h1) and og.verify_pin("4821", h2)
    assert not og.verify_pin("4820", h1), "неверный код не проходит"
    assert not og.verify_pin("", h1)
    assert not og.verify_pin("4821", None)
    assert not og.verify_pin("4821", "garbage")
    assert not og.verify_pin("4821", "md5$aa$bb"), "чужой алгоритм — не совпадение"


def test_lock_math():
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    assert og.is_locked(now + timedelta(minutes=5), now) is True
    assert og.is_locked(now - timedelta(minutes=5), now) is False
    assert og.is_locked(None, now) is False
    assert og.MAX_FAILS == 5 and og.LOCK_MINUTES == 15


def test_drain_alert_names_the_action_and_escapes():
    text = og.build_drain_alert("reveal_twofa", "паролей показано: 3")
    assert "показ сохранённых паролей 2FA" in text
    assert "паролей показано: 3" in text
    assert "Мои входы" in text
    bad = og.build_drain_alert("export_session", "<script>alert(1)</script>")
    assert "<script>" not in bad


# ── Проводка: калитка и сигнал стоят на опасных ручках ───────────────────────

def test_dangerous_endpoints_are_gated_and_alerted():
    api = _src("services/mini_app_api.py")
    # Показ всех паролей 2FA.
    tw = api[api.index("async def profile_setter_twofa("):api.index("# ── Account Cleaner")]
    assert "require_owner_pin(request, pool, uid)" in tw
    assert 'alert_owner_drain(pool, uid, "reveal_twofa"' in tw

    acc = _src("services/mini_app_accounts.py")
    # Экспорт строки сессии.
    assert "require_owner_pin(request, pool, uid)" in acc
    assert 'alert_owner_drain(pool, uid, "export_session"' in acc
    assert 'alert_owner_drain(pool, uid, "account_delete"' in acc

    md = _src("services/mini_app_my_data.py")
    assert "require_owner_pin(request, pool, uid)" in md
    assert 'alert_owner_drain(pool, uid, "data_delete"' in md


def test_gate_returns_403_need_pin_not_a_silent_pass():
    api = _src("services/mini_app_api.py")
    g = api[api.index("async def require_owner_pin("):api.index("def alert_owner_drain(")]
    assert '"need_pin": True' in g
    assert "status=403" in g
    # Калитка пускает, когда действие разрешено (кода нет или сеанс подтверждён).
    assert 'g.get("allowed")' in g and "return None" in g


def test_routes_and_frontend_are_wired():
    api = _src("services/mini_app_api.py")
    for r in ("/api/miniapp/owner/guard\", owner_guard_state",
              "/api/miniapp/owner/guard/set\", owner_guard_set",
              "/api/miniapp/owner/guard/remove\", owner_guard_remove",
              "/api/miniapp/owner/guard/verify\", owner_guard_verify"):
        assert r in api, r
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "POST /api/miniapp/owner/guard/verify" in snap

    html = _src("mini_app/index.html")
    assert "function ownGuardHtml" in html and "function askOwnerPin" in html
    # api() сам спрашивает код на need_pin и повторяет запрос — опасные ручки
    # не правятся поимённо.
    assert "need_pin" in html and "owner/guard/verify" in html
    # Подсказка на экране «Защита».
    assert "shieldPinNudge" in html and "pin_suggested" in html


def test_shield_summary_tells_whether_to_suggest_a_pin():
    api = _src("services/mini_app_api.py")
    s = api[api.index("async def shield_summary("):api.index("async def ad_intel_overview(")]
    assert '"pin_suggested": await _owner_guard.pin_suggested(pool, uid)' in s
