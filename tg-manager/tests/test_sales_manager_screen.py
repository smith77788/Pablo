"""Mini-App экран «Менеджер продаж» подключён и ведёт на реальные роуты.

Гарантирует, что кнопка настроек и экран-конструктор не «висят в воздухе»:
файл экрана подключён, функции определены, есть навигация назад, а api-вызовы
покрыты бэкендом (полнее — в test_miniapp_calls_have_routes).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _screen():
    return open(os.path.join(ROOT, "mini_app", "screens", "sales_manager.js"),
                encoding="utf-8").read()


def _index():
    return open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def test_screen_included_and_tile_present():
    idx = _index()
    assert '<script src="screens/sales_manager.js">' in idx
    assert "openSalesManager()" in idx          # кнопка настроек в меню


def test_screen_defines_entrypoints_and_uses_sales_api():
    js = _screen()
    for fn in ("function openSalesManager(", "function openPersonaEditor(",
               "function spSave(", "function spAddProduct(", "function spOrders("):
        assert fn in js, fn
    # ведёт на реальные роуты sales/*
    for route in ("/api/miniapp/sales/personas", "/api/miniapp/sales/persona",
                  "/api/miniapp/sales/persona/' + pid + '/assign",
                  "/api/miniapp/sales/persona/' + pid + '/product"):
        assert route in js, route


def test_screen_has_back_navigation_no_dead_end():
    js = _screen()
    # каждый экран рисует hdr с кнопкой назад — не тупик
    assert "onclick=\"back()\"" in js
    # сохранение и привязка бота реально вызываются
    assert "method: 'POST'" in js and "method: 'PATCH'" in js and "method: 'DELETE'" in js


def test_persona_test_diagnostic_wired():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    api = open(os.path.join(root, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "/api/miniapp/sales/persona/{pid}/test" in api
    js = _screen()
    assert "function spTest(" in js and "/test" in js
