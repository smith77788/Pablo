"""Подсказка «ИИ не подключён» знает, кто её читает.

Ключи ИИ платформенные — их задаёт администратор платформы в боте (/admin →
«🤖 AI-ключи»). А три экрана мини-аппа (виртуальный администратор каналов,
разогрев чатов, статус ИИ автоответчика) говорили ЛЮБОМУ владельцу «задайте
ключ в админ-меню». У владельца такого меню нет: совет вёл в тупик, и человек
искал настройку, которой у него не может быть.

Теперь текст один (`ai_providers.ai_missing_hint`) и зависит от роли:
администратору — где настройка, владельцу — чья она.
"""
from __future__ import annotations

import pathlib

from services.ai_providers import ai_missing_hint
from tests.miniapp_source import api_function, miniapp_source

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_owner_is_not_sent_to_a_menu_they_do_not_have():
    note = ai_missing_hint("флот молчит", admin=False)
    assert "/admin" not in note and "админ-меню" not in note
    assert "администратор платформы" in note and "флот молчит" in note


def test_admin_is_told_where_the_setting_lives():
    note = ai_missing_hint("флот молчит", admin=True)
    assert "/admin" in note and "AI-ключи" in note


def test_every_screen_takes_the_hint_by_role():
    assert "admin=_is_admin(uid)" in api_function("ai_status")
    warm = (ROOT / "services" / "mini_app_warmup.py").read_text(encoding="utf-8")
    assert "ai_missing_hint(" in warm and "admin=_is_admin(uid)" in warm
    va = (ROOT / "services" / "mini_app_va.py").read_text(encoding="utf-8")
    assert "network_overview(pool, uid, admin=_is_admin(uid))" in va


def test_no_screen_hardcodes_the_admin_menu_advice():
    src = miniapp_source()
    for bad in ("админ-меню «Ключи ИИ»", "не заданы ключи провайдеров ИИ"):
        assert bad not in src, f"экран снова советует владельцу недоступное: {bad}"
