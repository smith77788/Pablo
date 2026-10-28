"""Добавление прокси определяет страну (geo_country) автоматически.

Было: geo_country ставила только ручная кнопка в боте, а мини-апп добавлял
прокси с пустой страной — и гео-подбор аккаунтов/прокси и проверка гео-бана
ботов страну не видели (экран гео-бана показывал «различимо: 0» даже после
добавления прокси). Теперь add_proxy авто-определяет страну через единый
сервис services.proxy_geo, и на экране гео-бана есть кнопка «добавить прокси».
"""
from __future__ import annotations

import asyncio
import inspect

from tests.miniapp_source import api_function, js_function


def test_detect_proxy_geo_exists_and_is_safe():
    from services.proxy_geo import detect_proxy_geo
    assert inspect.iscoroutinefunction(detect_proxy_geo)
    # Пустой вход — без сети и без исключений: гео необязательно, сбой не роняет
    # «добавить прокси».
    assert asyncio.run(detect_proxy_geo("")) == {}
    assert asyncio.run(detect_proxy_geo("   ")) == {}


def test_add_proxy_autodetects_country():
    body = api_function("add_proxy")
    assert "from services.proxy_geo import detect_proxy_geo" in body
    assert "detect_proxy_geo(proxy_url" in body
    assert "UPDATE user_proxies SET geo_country=$1" in body
    # best-effort: определение гео не должно ронять добавление прокси
    assert "geo_country = None" in body


def test_bot_delegates_to_shared_detector():
    from bot.handlers import proxy_manager
    src = inspect.getsource(proxy_manager._detect_proxy_geo)
    assert "from services.proxy_geo import detect_proxy_geo" in src
    assert "return await detect_proxy_geo" in src


def test_geoban_screen_has_add_proxy():
    render = js_function("_shGeoRender")
    assert "shAddProxy()" in render
    assert "_shLoadBody()" in render  # кнопка обновления счётчика стран
    add = js_function("shAddProxy")
    assert "openProxyModal" in add
