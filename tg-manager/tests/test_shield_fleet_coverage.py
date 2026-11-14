"""Экран «Щит»: охват защиты по флоту (2FA-пароль, запасной ключ, чужие сеансы).

Тумблеры автозащиты говорят, что защита ВКЛЮЧЕНА, но не сколько аккаунтов реально
защищены сейчас — свежие и сбойные остаются дырой. Секция «Защита флота» — агрегат
тех же трёх сигналов, что в блоке «Безопасность» карточки аккаунта.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_shield_endpoint_returns_coverage():
    src = _src("services/mini_app_api.py")
    body = src[src.index("async def shield_summary("):]
    body = body[:body.index("\n    async def ", 1)]
    assert '"coverage": coverage' in body, "эндпоинт щита не отдаёт охват"
    for col in ("twofa_enc IS NOT NULL", "reserve_session_enc IS NOT NULL",
                "COALESCE(foreign_sessions, 0) > 0"):
        assert col in body, f"охват не считает {col}"
    # Только активные аккаунты и строго по владельцу.
    assert "FILTER (WHERE is_active)" in body and "WHERE owner_id=$1" in body


def test_shield_screen_renders_coverage():
    html = _src("mini_app/index.html")
    assert 'id="shieldCoverage"' in html, "нет контейнера охвата"
    fn = html[html.index("async function openShield("):]
    fn = fn[:fn.index("\nasync function ", 1)]
    assert "d.coverage" in fn, "openShield не читает охват"
    assert "Защита флота" in fn
    assert "Пароль 2FA сохранён" in fn and "Запасной ключ заведён" in fn
    # Дыры должны быть видно: «без защиты: N».
    assert "без защиты" in fn
