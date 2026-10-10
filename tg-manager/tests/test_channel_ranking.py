"""Замер позиций каналов/чатов: сопоставление нашего канала с выдачей поиска."""
from __future__ import annotations

from services import channel_ranking as R


def _res():
    return [
        {"position": 1, "channel_id": 111, "username": "Other", "title": "Чужой"},
        {"position": 2, "channel_id": 222, "username": "Dostavka_Moskva", "title": "Наш"},
        {"position": 3, "channel_id": 333, "username": "", "title": "Ещё"},
    ]


def test_find_by_channel_id():
    assert R.find_position(_res(), channel_id=222) == 2


def test_find_by_channel_id_with_100_prefix():
    # managed_channels может хранить -100…, выдача — чистый id: нормализуем оба
    assert R.find_position(_res(), channel_id=-1000000000222) == 2


def test_find_by_username_case_insensitive():
    assert R.find_position(_res(), username="@dostavka_moskva") == 2


def test_not_found_returns_none():
    assert R.find_position(_res(), channel_id=999, username="@nope") is None


def test_empty_results():
    assert R.find_position([], channel_id=222) is None


def test_normalize_keyword():
    assert R.normalize_keyword("  Доставка   Москва ") == "доставка москва"
    assert R.normalize_keyword("") == ""
