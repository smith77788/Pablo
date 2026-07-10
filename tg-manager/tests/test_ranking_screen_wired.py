"""Регрессия: экран «Рейтинг — позиции» (s-ranking) в mini-app был мёртвым —
HTML + бэкенд /api/miniapp/ranking/* добавили одним коммитом, но JS-функции
(openRanking/filterRanking/openRankingAddModal/submitRankingAdd) не написали →
клик по тайлу «Рейтинг» и кнопкам ничего не делал (ReferenceError). Достроено.

Тест фиксирует, что JS-функции экрана определены и что вызываемые ими маршруты
реально зарегистрированы на бэкенде (иначе — снова мёртвые кнопки).
"""
from __future__ import annotations

import inspect
import os
import re

from services import mini_app_api

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")


def _defined(html: str, name: str) -> bool:
    return bool(
        re.search(rf"\bfunction\s+{name}\s*\(", html)
        or re.search(rf"\b{name}\s*=\s*(?:async\s+)?function\b", html)
        or re.search(rf"\b{name}\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", html)
    )


def test_ranking_screen_functions_defined():
    html = open(_HTML, encoding="utf-8").read()
    for fn in ("openRanking", "filterRanking", "openRankingAddModal",
               "submitRankingAdd"):
        assert _defined(html, fn), f"JS-функция {fn} экрана Рейтинг должна быть определена"


def test_ranking_backend_routes_registered():
    src = inspect.getsource(mini_app_api)
    for path in ("/api/miniapp/ranking/positions", "/api/miniapp/ranking/stats",
                 "/api/miniapp/ranking/alerts", "/api/miniapp/ranking/track",
                 "/api/miniapp/ranking/untrack"):
        assert f'"{path}"' in src, f"маршрут {path} должен быть зарегистрирован"
