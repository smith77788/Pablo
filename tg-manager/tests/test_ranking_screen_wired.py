"""Регрессия: экран «Рейтинг — позиции» (s-ranking) в mini-app был мёртвым —
HTML + бэкенд /api/miniapp/ranking/* добавили одним коммитом, но JS-функции
(openRanking/filterRanking/openRankingAddModal/submitRankingAdd) не написали →
клик по тайлу «Рейтинг» и кнопкам ничего не делал (ReferenceError). Достроено.

Тест фиксирует, что JS-функции экрана определены и что вызываемые ими маршруты
реально зарегистрированы на бэкенде (иначе — снова мёртвые кнопки).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from services import mini_app_ranking
from tests.miniapp_routes import registered_routes

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
    """Проверяется РОУТЕР, а не текст исходника.

    Раньше здесь искали строку с путём в `mini_app_api`. Когда группа переехала
    в `services/mini_app_ranking.py`, тест покраснел на живых маршрутах — и это
    худший исход: проверка, которая срабатывает на переносе файла и молчит на
    настоящей потере маршрута, заставляет её ослабить. Теперь список снимается
    с собранного приложения, и ему всё равно, в каком модуле лежит обработчик.
    """
    routes = set(registered_routes())
    assert len(routes) > 800, "инвентарь маршрутов пуст — проверка измеряет не то"
    for method, path in (("GET", "/api/miniapp/ranking/positions"),
                         ("GET", "/api/miniapp/ranking/stats"),
                         ("GET", "/api/miniapp/ranking/alerts"),
                         ("POST", "/api/miniapp/ranking/track"),
                         ("POST", "/api/miniapp/ranking/untrack")):
        assert f"{method} {path}" in routes, (
            f"маршрут {method} {path} должен быть зарегистрирован")


def test_ranking_bare_overview_route_exists():
    """loadRanking (фронт) зовёт bare /api/miniapp/ranking — маршрут ДОЛЖЕН быть
    (иначе экран Рейтинг не грузит данные, 404). Отдаёт keywords+alerts."""
    assert "GET /api/miniapp/ranking" in set(registered_routes()), (
        "bare-маршрут /api/miniapp/ranking должен быть зарегистрирован")
    src = Path(mini_app_ranking.__file__).read_text("utf-8")
    m = re.search(r"async def ranking_overview\(.*?\n(.*?)    async def ",
                  src, re.DOTALL)
    assert m, "обработчик ranking_overview не найден"
    body = m.group(1)
    # Кавычки — вопрос стиля, а не контракта: проверяем ИМЕНА полей ответа.
    for field in ("keywords", "alerts"):
        assert re.search(rf"""["\']{field}["\']\s*:""", body), (
            f"ranking_overview должен отдавать {field} (форма для loadRanking)")
