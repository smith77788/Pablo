"""Регрессия: bot-паритет — нативный дашборд метрик в боте.

Раньше операторский дашборд был только в mini-app. Добавлен нативный /dashboard
+ пункт в меню «Аналитика», источник цифр — тот же get_dashboard_stats (реальные
данные). Проверяем: хендлер есть, роутер подключён в main.py, кнопка в меню есть.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_dashboard_handler_defined():
    src = _read("bot/handlers/metrics_dashboard.py")
    assert 'Command("dashboard")' in src
    assert 'BmCb.filter(F.action == "metrics_dashboard")' in src
    assert "get_dashboard_stats" in src           # реальные данные, тот же источник
    assert "async def _build_dashboard" in src


def test_router_registered_in_main():
    m = _read("main.py")
    assert "metrics_dashboard" in m and "include_router(_metrics_dashboard_handler.router)" in m


def test_menu_button_present():
    menu = _read("bot/handlers/botmother_menu.py")
    assert 'BmCb(action="metrics_dashboard")' in menu
    # у BmCb action=analytics есть хендлер (кнопка «Назад» из дашборда ведёт туда)
    assert 'F.action == "analytics"' in menu
