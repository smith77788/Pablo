"""Регресс: единая точка входа в дашборд.

Жалоба пользователя: «остался лишь один единый полноценный дашборд». Единый хаб
(`openUnifiedDashboard`, screens/dashboard.js — вкладки Инфраструктура/Аналитика/
Все дашборды) уже был, НО в меню/на Главной оставались ОТДЕЛЬНЫЕ верхнеуровневые
входы в старые дашборды (`onclick="openAnalytics()"`, `onclick="openAnalyticsDashboard()"`)
— т.е. по факту несколько дашбордов. Перенаправлены в единый хаб. Сами функции
НЕ удалены — их запускает вкладка «Все дашборды» хаба (_UD_LAUNCH), детальные
экраны доступны из одной точки.
"""
from __future__ import annotations

import os
import re

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")
_DASHJS = os.path.join(os.path.dirname(__file__), "..", "mini_app", "screens", "dashboard.js")


def _html() -> str:
    return open(_HTML, encoding="utf-8").read()


def test_no_toplevel_entry_to_old_separate_dashboards():
    html = _html()
    for dead in ('onclick="openAnalytics()"', 'onclick="openAnalyticsDashboard()"'):
        assert dead not in html, (
            f"верхнеуровневый вход в отдельный дашборд ({dead}) — должен вести в "
            "единый хаб openUnifiedDashboard"
        )


def test_unified_hub_is_the_entry():
    html = _html()
    assert 'onclick="openUnifiedDashboard()"' in html, "нет входа в единый дашборд-хаб"
    assert '<script src="screens/dashboard.js"></script>' in html, "хаб не подключён"


def test_detail_dashboards_still_reachable_from_hub():
    """Детальные экраны (НЕ дашборды) запускаются из вкладки «Все разделы».

    Второй «Дашборд метрик» (openAnalyticsDashboard) УДАЛён по правилу «один
    дашборд» — его метрики уже во вкладке «Аналитика» единого дашборда. Здесь
    проверяем, что легитимные детальные экраны на месте."""
    html = _html()
    assert "function openAnalytics()" in html
    dashjs = open(_DASHJS, encoding="utf-8").read()
    # лаунчер хаба ссылается на детальные экраны (без второго дашборда)
    assert "openAnalytics" in dashjs and "openHealth" in dashjs
