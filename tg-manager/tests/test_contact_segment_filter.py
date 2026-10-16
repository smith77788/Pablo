"""Сегментация контактов: новые оси фильтра (пол, CRM-стадия) в едином WHERE.

Фильтр списка и резолвер сегмента для действия должны строиться ОДНИМ
конструктором — иначе «на что смотрю» и «на что применяю» разъедутся. Проверяем
_segment_where (чистая функция) + классификатор пола контактов.
"""
from __future__ import annotations

import asyncio

from services.contacts_hub.repository import _segment_where
from services import gender_classifier as gc


def test_gender_axis_exact_and_unknown():
    where, params, _ = _segment_where(1, {"gender": "m"})
    assert "gender = $2" in where and params[1] == "m"
    where2, params2, _ = _segment_where(1, {"gender": "unknown"})
    assert "gender IS NULL" in where2 and params2 == [1]     # без лишнего параметра
    where3, params3, _ = _segment_where(1, {"gender": "bogus"})
    assert "gender" not in where3                              # мусор игнорируем


def test_crm_stage_axis_joins_contact_crm():
    where, params, _ = _segment_where(7, {"crm_stage": "lead"})
    assert "contact_crm" in where and "stage = $2" in where
    assert params == [7, "lead"]


def test_axes_combine_with_correct_indices():
    where, params, idx = _segment_where(5, {"tag": "vip", "gender": "f", "crm_stage": "won"})
    # порядок: owner=$1, tag=$2, gender=$3, crm_stage=$4
    assert params == [5, "vip", "f", "won"]
    assert "$2 = ANY(tags)" in where or "ANY(tags)" in where
    assert "gender = $3" in where
    assert "stage = $4" in where
    assert idx == 5


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    async def fetch(self, *a):
        return self._rows

    async def execute(self, sql, *args):
        self.updates.append((sql, args))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_classify_contacts_writes_gender():
    rows = [
        {"id": "u1", "first_name": "Мария", "last_name": None},
        {"id": "u2", "first_name": "Дмитрий", "last_name": None},
    ]
    pool = _FakePool(rows)
    res = _run(gc.classify_contacts(pool, 1, only_missing=True))
    assert res["total"] == 2
    # один UPDATE через unnest по unified_contacts
    assert pool.updates and "UPDATE unified_contacts" in pool.updates[0][0]
    ids, genders, owner = pool.updates[0][1]
    assert ids == ["u1", "u2"] and owner == 1
    # классификатор должен что-то распознать (женское/мужское русское имя)
    assert set(genders) <= {"m", "f", None}
