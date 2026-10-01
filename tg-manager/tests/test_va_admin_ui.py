"""Виртуальный администратор виден в интерфейсе и пишет числа по-русски.

Вход жил только кнопкой внутри «Каналов» — из раздела «Ещё» администратора было
не найти. Числа на его экранах шли через общий num(), который пишет «1.2K», и
склонялись как «3 постов».
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_more_screen_has_admin_tile():
    html = _read("mini_app", "index.html")
    more = html[html.index('<div class="screen" id="s-more">'):]
    more = more[:more.index('<div class="screen"', 10)]
    assert 'onclick="openVaAdmin()"' in more


def test_admin_screens_do_not_use_latin_number_format():
    js = _read("mini_app", "screens", "va_admin.js")
    assert not re.search(r"(?<![\w.])num\(", js), "num() даёт «1.2K» — латиница в интерфейсе"
    assert "' постов</span>" not in js and "пост.)" not in js


def test_ai_banner_says_where_to_enter_key(monkeypatch):
    """Плашка «ИИ не подключён» обязана сказать, ГДЕ ввести ключ: иначе владелец
    видит запрет без выхода (ключ задаётся только в боте, в /admin)."""
    from services import channel_admin as ca, ai_claude
    from services import ai_providers
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(ai_providers, "configured_providers", lambda: [])
    ok, note = ca.ai_ready()
    assert not ok and "/admin" in note and "AI-ключи" in note
