"""Регрессия: экраны Analytics Dashboard и Audience Analytics больше не 404.

Были «несуществующие разделы»: фронт звал /dashboard_realtime и /audience_analytics,
роутов не было (+ баг вызова фронта method:'?'+params). Фикс: эндпоинты возвращают
ТОЧНУЮ форму, которую читает экран, реальными данными (каналы/аудитория/операции/
активность), пустыми — где система статистику не собирает (просмотры/история графиков).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_dashboard_endpoint_route_and_shape():
    api = _read("services/mini_app_api.py")
    assert "async def dashboard_realtime" in api
    assert 'add_get("/api/miniapp/dashboard_realtime", dashboard_realtime)' in api
    seg = api[api.index("async def dashboard_realtime"):api.index("async def audience_analytics")]
    # все поля, которые читает экран, присутствуют в ответе
    for f in ["total_subscribers", "total_channels", "total_posts", "total_views",
              "growth_7d", "subs_history", "views_history", "engagement_history",
              "top_channels", "recent_activity", "total_accounts", "total_operations"]:
        assert f'"{f}"' in seg, f"нет поля {f}"
    # тайм-серии считаются из РЕАЛЬНЫХ таблиц (bot_users/operation_audit), не пусто
    assert "FROM bot_users" in seg and "FROM operation_audit" in seg
    assert "date_trunc('day'" in seg  # дневные серии
    assert "_fill(" in seg            # заполнение пропусков нулями (непрерывный график)


def test_audience_endpoint_route_and_shape():
    api = _read("services/mini_app_api.py")
    assert "async def audience_analytics" in api
    assert 'add_get("/api/miniapp/audience_analytics", audience_analytics)' in api
    seg = api[api.index("async def audience_analytics"):api.index("async def operation_export")]
    for f in ["total_users", "active_users", "avg_engagement", "segments",
              "insights", "heatmap"]:
        assert f'"{f}"' in seg, f"нет поля {f}"


def test_frontend_dashboard_call_bug_fixed():
    """Параметры уезжают в URL, а не в объект опций.

    Старый баг: `api('/dashboard_realtime', {method: '?' + params})` — строка
    запроса подставлялась в HTTP-метод, сервер получал запрос без параметров.

    Раньше проверка смотрела только в index.html и искала точное написание
    через `new URLSearchParams`. Оба вызова оттуда удалены вместе со вторым
    «Дашбордом метрик» (недостижимый экран `s-analytics-dashboard`), живой
    вызов переехал в `screens/dashboard.js` и пишет параметр прямо в URL.
    Смысл проверки прежний, поэтому она теперь смотрит во ВЕСЬ мини-апп и
    требует параметры в URL, не диктуя способ их сборки."""
    from tests.miniapp_source import miniapp_source

    ui = miniapp_source()
    # старый баг (method вместо URL) — нигде в мини-аппе
    assert "dashboard_realtime', { method: '?'" not in ui
    assert "dashboard_realtime\", { method: \"?" not in ui
    # живой вызов передаёт параметры строкой запроса
    assert "dashboard_realtime?" in ui, "экран аналитики больше не зовёт эндпоинт"
