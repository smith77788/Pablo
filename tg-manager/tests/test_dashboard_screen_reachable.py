"""Экран сводки: 500 строк аналитики лежали без единого входа.

`services/analytics_dashboard.py` считает аккаунты, операции, аудиторию и
здоровье — и по суткам, и рядами по дням за период. Три маршрута
(`/analytics/dashboard`, `/analytics/realtime`, `/analytics/historical`) с
фронта не звал никто: посмотреть на это было негде.

Экран обязан быть честным про источник: у каждой метрики свой ключ в ответе
(`total`, `new_users`, `avg_health`), пустой период — это «записей нет», а не
ноль, и ошибка запроса — не «нулевая аналитика».

Поле `revenue_30d_usd` экран сознательно НЕ показывает: запрос суммирует
`payments WHERE user_id = владелец`, то есть деньги, которые владелец ЗАПЛАТИЛ
сам, а не заработал. Подписать это «выручкой» было бы враньём.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
MOD = os.path.join(ROOT, "services", "analytics_dashboard.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_there_is_a_way_in():
    h = _html()
    assert 'onclick="openDashboard()"' in h, "на сводку нельзя попасть ни с одного экрана"
    assert 'id="s-dashboard"' in h, "экрана сводки нет"


def test_screen_reads_both_routes():
    body = _js_func("loadDashboard")
    assert "/api/miniapp/analytics/dashboard" in body
    series = _js_func("loadDashboardSeries")
    assert "/api/miniapp/analytics/historical" in body + series
    assert "metric=" in series and "days=" in series, "период и метрика не выбираются"


def test_every_metric_reads_its_own_key():
    """У метрик разные поля в ответе; общий `value` вернул бы нули везде."""
    h = _html()
    m = re.search(r"const DASH_METRICS\s*=\s*\{(.*?)\n\};", h, re.S)
    assert m, "таблицы метрик нет"
    table = m.group(1)
    with open(MOD, encoding="utf-8") as f:
        mod = f.read()
    for metric, key in (("operations", "total"), ("accounts", "total"),
                        ("audience", "new_users"), ("health", "avg_health")):
        assert metric in table, f"метрика {metric} не показывается"
        assert f'"{key}"' in mod, f"сервер перестал отдавать {key} — экран покажет нули"
    assert re.search(r"key\s*:\s*'new_users'", table), "аудитория читает чужой ключ"
    assert re.search(r"key\s*:\s*'avg_health'", table), "здоровье читает чужой ключ"


def test_source_of_numbers_is_named():
    """Цифра без источника — утверждение, которое нечем проверить."""
    h = _html()
    m = re.search(r"const DASH_METRICS\s*=\s*\{(.*?)\n\};", h, re.S)
    table = m.group(1)
    assert table.count("src:") == 4, "не у каждой метрики назван источник"
    assert "dashSeriesNote" in _js_func("loadDashboardSeries"), "источник не выводится на экран"


def test_empty_period_is_not_zero_and_error_is_not_empty():
    body = _js_func("loadDashboardSeries")
    assert "записей нет" in body, "пустой период выдаётся за ноль"
    assert "errHtml" in body, "ошибка запроса выдаётся за пустую аналитику"
    assert "errHtml(e.message, 'loadDashboard()')" in _js_func("loadDashboard")


def test_owner_payments_are_not_called_revenue():
    """payments.user_id = владелец — это его расходы, не выручка."""
    h = _html()
    assert "revenue_30d_usd" not in h, (
        "на экран попала «выручка», которая на деле — платежи владельца")


def test_chart_has_a_scale():
    body = _js_func("dashDrawChart")
    assert "fillText(String(max)" in body.replace(" ", "") or "String(max)" in body, (
        "линия без подписанного масштаба — украшение, а не график")
    assert "shortDate" in body, "на графике не видно, за какие дни он построен"
