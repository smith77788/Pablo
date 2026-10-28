"""Доступ к модулю «Массовые жалобы» в мини-аппе = ПАРИТЕТ с ботом.

Баг: бот (bot.handlers.strike._has_access) даёт доступ платформенному
админу и enterprise БЕЗ строки в strike_access, а мини-апп
(mini_app_protection) проверял ТОЛЬКО таблицу strike_access. Владелец/админ
видел модуль в боте, но в мини-аппе получал «Нет доступа к Strike».

Фикс: оба гейта мини-аппа (strike_status, strike_launch) идут через
_strike_has_access, который переиспользует ту же функцию доступа, что и бот.
"""
from __future__ import annotations

import asyncio

from tests.miniapp_source import api_function, api_source


def test_strike_status_uses_admin_aware_access():
    window = api_function("strike_status")
    assert "_strike_has_access(uid)" in window, (
        "strike_status должен считать доступ через _strike_has_access "
        "(паритет с ботом), а не только по строке strike_access")
    # has_access больше не берётся из голой проверки таблицы
    assert "has_access = await _strike_has_access(uid)" in window


def test_strike_launch_uses_admin_aware_access():
    window = api_function("strike_launch")
    assert "if not await _strike_has_access(uid):" in window, (
        "strike_launch должен гейтить через _strike_has_access")
    # старый гейт «только таблица» ушёл
    assert 'SELECT mode FROM strike_access WHERE user_id=$1' not in window


def test_helper_reuses_bot_access_logic():
    src = api_source()
    assert "async def _strike_has_access" in src
    assert "from bot.handlers.strike import _has_access" in src, (
        "единый источник правды — функция доступа бота, а не копия логики")


def test_bot_access_grants_platform_admin_without_table_row(monkeypatch):
    """Контракт, на который опирается фикс: у платформенного админа доступ есть
    БЕЗ строки в strike_access. Если это сломать, мини-апп снова начнёт пускать
    не того (или не пускать владельца)."""
    import bot.utils.subscription as subs
    from bot.handlers import strike
    monkeypatch.setattr(subs, "is_platform_admin", lambda _uid: True)
    # pool не нужен: is_platform_admin=True короткозамыкает до запросов к БД
    assert asyncio.run(strike._has_access(None, 777)) is True
