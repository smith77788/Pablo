"""Сегментация аудитории считается одним запросом и по-русски.

ЧТО БЫЛО. `segment_audience` тянула КАЖДОГО подписчика строкой, а на каждого
делала ещё один запрос за числом взаимодействий. На боте с полусотней тысяч
подписчиков это полсотни тысяч запросов на открытие экрана — открыть его было
нельзя. Дальше сегменты считались перебором списков с проверкой
`u not in champions`, то есть квадратом от числа подписчиков.

Числа при этом всё равно были неверные: «взаимодействия» считались как
КОЛИЧЕСТВО СТРОК user_activity для пары «бот + человек», а строка там одна.
Значит взаимодействий всегда выходило ровно одно, и сегменты с порогами 50 и 20
не могли появиться ни при каких данных. Настоящее число лежит рядом, в колонке
message_count.

Ко всему этому сегменты назывались Champions, Loyal, At Risk, Dormant, New —
по-английски, а их видит владелец, который английского не знает.

ЧТО ТЕПЕРЬ. Один запрос со счётчиками FILTER, русские названия, пороги —
константы модуля (разошедшиеся копии порога однажды уже сделали сегмент
недостижимым).
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import audience_analytics as aa


class _RecordingPool:
    """Пул, который считает обращения и отдаёт одну заранее заданную строку."""

    def __init__(self, row):
        self._row = row
        self.calls: list[str] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        return self._row

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        return []

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        return 0


_ROW = {
    "total": 100,
    "leaders": 5, "leaders_msgs": 120.0,
    "regulars": 20, "regulars_msgs": 35.0,
    "at_risk": 10, "at_risk_msgs": 8.0,
    "dormant": 60,
    "newcomers": 7, "newcomers_msgs": 2.0,
}


async def test_segmentation_is_a_single_query():
    pool = _RecordingPool(_ROW)
    segments = await aa.segment_audience(pool, owner_id=1)
    assert len(pool.calls) == 1, (
        f"сегментация сделала {len(pool.calls)} запросов вместо одного — "
        "вернулся запрос на каждого подписчика")
    assert segments, "при непустых данных сегменты обязаны быть"


async def test_no_per_user_engagement_query_left():
    src = inspect.getsource(aa)
    for gone in ("_get_user_engagements", "_fetch_user_activity"):
        assert gone not in src, (
            f"{gone} вернулся: это запрос на каждого подписчика")


async def test_segment_names_are_russian():
    pool = _RecordingPool(_ROW)
    for seg in await aa.segment_audience(pool, owner_id=1):
        assert not re.search(r"[A-Za-z]", seg.name), (
            f"сегмент «{seg.name}» назван латиницей — владелец не читает "
            "по-английски")
        assert seg.description and not re.search(r"[A-Za-z]", seg.description)


async def test_thresholds_are_shared_between_sql_and_names():
    """Порог не продублирован строкой: разошедшиеся копии ломали сегмент."""
    assert aa._LEADER_MSGS > aa._REGULAR_MSGS
    assert aa._AT_RISK_DAYS > aa._REGULAR_DAYS > aa._LEADER_DAYS
    # В SQL пороги приходят параметрами, а не числами в тексте.
    assert str(aa._LEADER_MSGS) not in aa._SEGMENTS_SQL
    assert "message_count" in aa._SEGMENTS_SQL, (
        "число сообщений снова считается не из message_count")


async def test_owner_scope_and_optional_bot_filter():
    sql = aa._SEGMENTS_SQL
    assert "mb.added_by = $1" in sql, "сегменты считаются без привязки к владельцу"
    assert "$2::bigint IS NULL OR ua.bot_id = $2" in sql, (
        "пропал разбор «все боты владельца / один бот»")


async def test_empty_audience_gives_no_segments():
    pool = _RecordingPool({"total": 0, "leaders": 0, "leaders_msgs": None,
                           "regulars": 0, "regulars_msgs": None,
                           "at_risk": 0, "at_risk_msgs": None, "dormant": 0,
                           "newcomers": 0, "newcomers_msgs": None})
    assert await aa.segment_audience(pool, owner_id=1) == []


async def test_percentages_sum_within_the_measured_group():
    pool = _RecordingPool(_ROW)
    segments = await aa.segment_audience(pool, owner_id=1)
    by = {s.name: s for s in segments}
    assert by["Лидеры"].percentage == 5.0
    assert by["Спящие"].percentage == 60.0
    assert by["Лидеры"].avg_engagement == 120.0


def test_screen_uses_the_real_segmentation():
    """Экран «Аудитория+» обязан брать сегменты из модуля, а не считать свои."""
    src = open("services/mini_app_api.py", encoding="utf-8").read()
    i = src.index("async def audience_analytics(")
    seg = src[i:src.index("\n    async def ", i + 10)]
    assert "from services.audience_analytics import segment_audience" in seg, (
        "экран снова считает три грубые доли вместо настоящей сегментации")
    assert "await segment_audience(pool, uid)" in seg
