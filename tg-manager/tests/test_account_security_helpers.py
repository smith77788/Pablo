"""Регрессия: чистые хелперы функций безопасности аккаунта (Telegram Expert-паритет).

Покрывает извлечение кода входа (extract_login_code) и набор ключей приватности.
Telethon в песочнице недоступен, поэтому проверяем только чистую логику —
сами set_privacy/close_other_sessions/get_login_code ходят в сеть и тестируются
интеграционно, но разбор кода вынесен в чистую функцию именно ради теста.
"""
from __future__ import annotations

from services.profile_setter_engine import extract_login_code, _PRIVACY_KEYS


def test_extract_login_code_near_keyword():
    # код рядом со словом code/код имеет приоритет над посторонними числами
    assert extract_login_code("Login code: 12345. Do not share.") == "12345"
    assert extract_login_code("Ваш код входа: 654321") == "654321"


def test_extract_login_code_six_digits():
    assert extract_login_code("Code 987654 is your login code") == "987654"


def test_extract_login_code_fallback_isolated_block():
    # нет слова code/код — берём изолированный 5–6-значный блок
    assert extract_login_code("Telegram: 44556") == "44556"


def test_extract_login_code_ignores_long_numbers():
    # номер телефона / id не должны ловиться как код (7+ цифр без границы 5-6)
    assert extract_login_code("Call +79001234567 now") is None


def test_extract_login_code_none_on_empty_or_no_digits():
    assert extract_login_code("") is None
    assert extract_login_code(None) is None  # type: ignore[arg-type]
    assert extract_login_code("no digits here") is None


def test_privacy_keys_contract():
    # ключи приватности, которые понимает set_privacy и валидирует API-слой
    assert _PRIVACY_KEYS == {"phone", "invite", "lastseen"}
