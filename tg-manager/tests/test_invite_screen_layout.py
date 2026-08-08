"""Регрессия: экран «Массовый инвайт» — «лишние детали и кривой» график.

Жалоба (скриншот): недельный график флота рисовался как 1–2 толстые полосы во всю
ширину (backend возвращал ТОЛЬКО дни с данными через GROUP BY), а блок аналитики +
«Разбор» занимал весь первый экран до самой настройки инвайта.

Фиксы:
  1. backend invite_analytics.week — плотный ряд из 7 дней через generate_series
     (дни без активности = 0) → всегда 7 выровненных столбцов;
  2. фронт — столбцы фиксированной узкой ширины (max-width), пустые дни без высоты;
  3. аналитика и «Разбор» свёрнуты в <details> (по умолчанию закрыто) — настройка
     инвайта сразу на виду.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "mini_app" / "index.html"
    return p.read_text(encoding="utf-8")


def test_week_query_is_dense_7_days():
    """week должен строиться на generate_series — иначе редкие дни дают «кривой»
    график из 1–2 полос во всю ширину."""
    src = _api_src()
    m = re.search(r"async def invite_analytics\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "invite_analytics handler not found"
    body = m.group(1)
    assert "generate_series" in body, (
        "week должен возвращать плотный 7-дневный ряд (generate_series), "
        "а не только дни с данными"
    )


def test_analytics_collapsed_into_details():
    """Аналитика флота + «Разбор» свёрнуты в <details> — убираем «лишние детали»
    с первого экрана инвайта."""
    html = _index_html()
    m = re.search(r'<summary[^>]*>📊 Аналитика флота[^<]*</summary>(.*?)</details>', html, re.DOTALL)
    assert m, "блок аналитики инвайта должен быть внутри свёрнутого <details>"
    inner = m.group(1)
    assert 'id="invAnalytics"' in inner and 'id="invAdvice"' in inner, (
        "и аналитика, и «Разбор» должны быть внутри свёрнутого блока"
    )


def test_week_bars_have_capped_width():
    """Столбцы графика — фиксированной узкой ширины (max-width), иначе 2 дня
    растягиваются на пол-ширины каждый."""
    html = _index_html()
    # ищем участок рендера столбцов недели
    assert re.search(r"while \(wk\.length < 7\) wk\.unshift", html), (
        "график должен дополняться до 7 столбцов"
    )
    assert "max-width:14px" in html, "ширина столбца недели должна быть ограничена"
