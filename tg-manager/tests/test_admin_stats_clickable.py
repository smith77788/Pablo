"""Плитки админ-статистики должны быть кликабельными (баг: read-only меню).

Раньше плитки Юзеры/Подписки/Операции были просто показателями без onclick —
пользователь ожидал drill-down. Проверяем, что ключевые плитки ведут в
существующие экраны (openAdminUsers/openOps), а сами функции есть.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_stats_tiles_are_clickable():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    body = html[html.index("async function openAdminStats"):]
    body = body[:body.index("async function openAdminUsers")]
    # Юзеры/Подписки → список пользователей; операции → экран операций
    assert "openAdminUsers()" in body, "плитки юзеров/подписок не кликабельны"
    assert "openOps()" in body, "плитки операций не кликабельны"
    assert "cursor:pointer" in body, "нет визуального признака кликабельности"
    # целевые функции реально существуют
    assert "function openAdminUsers(" in html
    assert "function openOps(" in html
