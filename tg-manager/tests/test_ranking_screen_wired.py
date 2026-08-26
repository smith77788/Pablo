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


def test_ranking_bare_overview_route_exists():
    """loadRanking (фронт) зовёт bare /api/miniapp/ranking — маршрут ДОЛЖЕН быть
    (иначе экран Рейтинг не грузит данные, 404). Отдаёт keywords+alerts."""
    src = inspect.getsource(mini_app_api)
    assert re.search(r'add_get\(\s*"/api/miniapp/ranking"\s*,\s*ranking_overview', src), (
        "bare-маршрут /api/miniapp/ranking должен быть зарегистрирован"
    )
    m = re.search(r"async def ranking_overview\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "ranking_overview handler not found"
    body = m.group(1)
    # Кавычки — вопрос стиля, а не контракта: проверяем ИМЕНА полей ответа.
    for field in ("keywords", "alerts"):
        assert re.search(rf"""["']{field}["']\s*:""", body), (
            f"ranking_overview должен отдавать {field} (форма для loadRanking)")
