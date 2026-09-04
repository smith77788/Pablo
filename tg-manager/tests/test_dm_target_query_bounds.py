"""Выборка аудитории кампании: отсечение в SQL и потолок.

Что было: вся аудитория грузилась в память целиком, а уже обработанные
получатели отсеивались только в Python. На базе в сотни тысяч контактов это
тяжёлый старт кампании и лишние десятки мегабайт; потолка не было ни у одного
источника, кроме сегмента.

Тест собирает РЕАЛЬНЫЕ запросы через подставной пул и проверяет их форму:
согласованность плейсхолдеров с аргументами (рассинхрон уронил бы отправку
уже в бою), наличие потолка и отсечения, и совместимость DISTINCT ON с
ORDER BY (Postgres требует, чтобы DISTINCT ON шёл первым в сортировке).
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import dm_engine

_CASES = [
    ("bot_users", 5, {}),
    ("crm", None, {}),
    ("all_bots", None, {}),
    ("cohort", 5, {"cohort_type": "warm"}),
    ("parsed_audience", 0, {}),
    ("parsed_audience", 7, {"gender_filter": "m"}),
]


class _CapturePool:
    def __init__(self):
        self.audience_query = None
        self.audience_args = ()

    async def fetch(self, query, *args):
        if "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status" in query:
            return []          # журнал отправок — пуст
        self.audience_query, self.audience_args = query, args
        return []

    async def execute(self, *a, **kw):
        return "OK"


def _build(target_type, target_id, params):
    pool = _CapturePool()
    campaign = {"id": 42, "owner_id": 1, "target_type": target_type,
                "target_id": target_id, "params": params}
    asyncio.run(dm_engine._get_targets(pool, campaign))
    assert pool.audience_query, f"запрос аудитории не собран для {target_type}"
    return pool.audience_query, pool.audience_args


@pytest.mark.parametrize("target_type,target_id,params", _CASES)
def test_placeholders_match_arguments(target_type, target_id, params):
    """Рассинхрон номеров $N и аргументов — отказ уже в бою, на живой рассылке."""
    q, args = _build(target_type, target_id, params)
    nums = sorted({int(n) for n in re.findall(r"\$(\d+)", q)})
    assert nums == list(range(1, len(args) + 1)), (nums, len(args), q)


@pytest.mark.parametrize("target_type,target_id,params", _CASES)
def test_query_is_bounded(target_type, target_id, params):
    q, _ = _build(target_type, target_id, params)
    assert f"LIMIT {dm_engine._MAX_CAMPAIGN_TARGETS}" in q


@pytest.mark.parametrize("target_type,target_id,params", _CASES)
def test_already_processed_excluded_in_sql(target_type, target_id, params):
    q, _ = _build(target_type, target_id, params)
    assert "dm_campaign_log" in q
    assert "'sent','blocked','skip'" in q.replace(" ", "")


@pytest.mark.parametrize("target_type,target_id,params", _CASES)
def test_distinct_on_matches_order_by(target_type, target_id, params):
    """Postgres отклоняет запрос, если DISTINCT ON не первый в ORDER BY —
    тот же класс ошибки, что обнулял список каналов."""
    q, _ = _build(target_type, target_id, params)
    m = re.search(r"DISTINCT ON \((\S+?)\)", q)
    if not m:
        pytest.skip("без DISTINCT ON")
    order = re.search(r"ORDER BY ([^\s,]+)", q)
    assert order and order.group(1) == m.group(1), q


def test_python_filter_kept_as_second_line():
    """Отсечение в SQL — основной путь, но фильтр по sent_ids оставлен: он
    страхует источники, где подзапрос неприменим (свой список получателей)."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "dm_engine.py").read_text(encoding="utf-8")
    assert "not in sent_ids" in src


def test_segment_and_others_share_the_same_cap():
    assert dm_engine._MAX_CAMPAIGN_TARGETS == dm_engine._SEGMENT_TARGET_LIMIT
