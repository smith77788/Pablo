"""Шаг 3 усиления «Модели поведения»: аналитика и тренды.

Не «где аудитория сейчас» (overview), а КАК она движется: конверсия воронки
(сколько интереса доходит до «готов купить»), моментум 7 дней против прошлых
(греемся/остываем), лучшие источники (какой бот довёл больше людей наверх).
Чистые помощники отделены от БД и проверяются без Postgres; саму analytics()
гоняем на фейковом пуле.
"""
from __future__ import annotations

import asyncio
import os

from services import virtual_layer as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── конверсия: из макс. достигнутого ранга в воронку «дошли до ступени» ────────

def test_conversion_counts_by_max_rank_not_by_sum():
    # вошедших в каждую ступень суммировать нельзя (задвоит одного), считаем по
    # максимуму: 10 дошли максимум до «интерес», 5 до «готов», 2 до «купил».
    rows = [{"maxr": 2, "c": 10}, {"maxr": 4, "c": 5}, {"maxr": 5, "c": 2}]
    conv = V.conversion_from_maxranks(rows)
    by = {c["value"]: c for c in conv}
    assert by["curious"]["reached"] == 17 and by["curious"]["conv_pct"] is None
    assert by["interested"]["reached"] == 17 and by["interested"]["conv_pct"] == 100
    assert by["qualified"]["reached"] == 7 and by["qualified"]["conv_pct"] == 41
    assert by["ready"]["reached"] == 7 and by["ready"]["conv_pct"] == 100
    assert by["purchased"]["reached"] == 2 and by["purchased"]["conv_pct"] == 29


def test_conversion_safe_on_garbage_rows():
    conv = V.conversion_from_maxranks([{"x": 1}, {"maxr": None, "c": 3}, {}])
    assert all(c["reached"] == 0 for c in conv)
    assert [c["value"] for c in conv] == [v for v, _ in V.CONVERSION_RUNGS]


# ── моментум: 7 дней против прошлых 7 ─────────────────────────────────────────

def test_momentum_heating_when_more_hot_and_not_more_lost():
    m = V.momentum_delta({"hot_now": 5, "hot_prev": 2, "lost_now": 1, "lost_prev": 3})
    assert m["verdict"] == "heating"
    assert m["hot_dir"] == "up" and m["lost_dir"] == "down"


def test_momentum_cooling_when_fewer_hot_and_not_fewer_lost():
    m = V.momentum_delta({"hot_now": 1, "hot_prev": 4, "lost_now": 5, "lost_prev": 2})
    assert m["verdict"] == "cooling"


def test_momentum_mixed_and_safe_on_missing():
    assert V.momentum_delta({"hot_now": 3, "hot_prev": 3,
                             "lost_now": 0, "lost_prev": 0})["verdict"] == "mixed"
    m = V.momentum_delta({})       # пустая строка не роняет
    assert m["hot_now"] == 0 and m["verdict"] == "mixed"


# ── подпись источника ─────────────────────────────────────────────────────────

def test_source_label_resolves_bot():
    assert V._source_label("bot_17", "mybot") == "@mybot"
    assert V._source_label("bot_17", None) == "Бот 17"
    assert V._source_label("manual") == "manual"
    assert V._source_label(None) == "—"


# ── analytics() на фейковом пуле ──────────────────────────────────────────────

class _Pool:
    def __init__(self, maxrank_rows, momentum_row, source_rows):
        self._maxrank = maxrank_rows
        self._momentum = momentum_row
        self._source = source_rows

    async def fetch(self, q, *a):
        if "MAX(CASE to_value" in q:
            return self._maxrank
        if "vs.source" in q:
            return self._source
        return []

    async def fetchrow(self, q, *a):
        if "hot_now" in q:
            return self._momentum
        return None


def test_analytics_assembles_all_three_blocks():
    pool = _Pool(
        maxrank_rows=[{"maxr": 2, "c": 4}, {"maxr": 4, "c": 2}],
        momentum_row={"hot_now": 3, "hot_prev": 1, "lost_now": 0, "lost_prev": 1},
        source_rows=[{"source": "bot_7", "c": 5, "bot_name": "shopbot"},
                     {"source": "manual", "c": 1, "bot_name": None}],
    )
    out = asyncio.run(V.analytics(pool, 7))
    # конверсия посчитана
    assert {c["value"] for c in out["conversion"]} == {v for v, _ in V.CONVERSION_RUNGS}
    assert out["conversion"][0]["reached"] == 6      # дошли хотя бы до «присматривается»
    # моментум — греемся
    assert out["momentum"]["verdict"] == "heating"
    # источники с подписями
    assert out["by_source"][0]["label"] == "@shopbot" and out["by_source"][0]["count"] == 5
    assert out["by_source"][1]["label"] == "manual"


def test_analytics_fail_open_on_broken_pool():
    class _Boom:
        async def fetch(self, q, *a):
            raise RuntimeError("база лежит")

        async def fetchrow(self, q, *a):
            raise RuntimeError("база лежит")
    out = asyncio.run(V.analytics(_Boom(), 7))
    assert out == {"conversion": [], "momentum": None, "by_source": []}


# ── проводка: маршрут, экран ──────────────────────────────────────────────────

def test_analytics_route_and_handler_wired():
    from tests.miniapp_source import api_source
    api = api_source()
    assert 'add_get("/api/miniapp/vlayer/analytics"' in api
    assert "await _vl.analytics(pool, uid)" in api


def test_screen_renders_trends_section():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "/api/miniapp/vlayer/analytics" in html
    assert "_vlAnalyticsHtml" in html
    assert "Конверсия воронки" in html and "Лучшие источники" in html
