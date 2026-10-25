"""Взаимопиар каналов одного владельца (cross-promo).

Канал изредка советует подписчикам родственный канал того же владельца — это
легальный рост сети (не третьи лица, не спам), пост идёт через редакторский гейт.
Проверяем чистую логику каденции и выбора канала, текст директивы и настройку.
"""
from __future__ import annotations

from services import channel_admin as ca


def test_should_cross_promote_cadence():
    assert ca.should_cross_promote(0) is False          # не на первом посте
    assert ca.should_cross_promote(ca.CROSS_EVERY) is True
    assert ca.should_cross_promote(ca.CROSS_EVERY - 1) is False
    assert ca.should_cross_promote(4, every=4) is True
    assert ca.should_cross_promote(2, every=4) is False


def test_pick_cross_sibling_round_robin_and_linkable_only():
    sibs = [{"username": "a"}, {"username": ""}, {"username": "b"}]
    # пустой username пропускаем, остальные — по кругу
    assert ca.pick_cross_sibling(sibs, 0)["username"] == "a"
    assert ca.pick_cross_sibling(sibs, 1)["username"] == "b"
    assert ca.pick_cross_sibling(sibs, 2)["username"] == "a"
    assert ca.pick_cross_sibling([{"username": ""}], 0) is None
    assert ca.pick_cross_sibling([], 0) is None


def test_cross_promo_topic_has_link_and_no_fabrication_rule():
    t = ca.cross_promo_topic({"username": "@MyChan", "title": "Мой канал", "topic": "финансы"})
    assert "t.me/MyChan" in t
    assert "Мой канал" in t
    assert "финансы" in t
    assert "Не выдумывай" in t


def test_settings_public_cross_promote_default_and_value():
    assert ca.settings_public(None)["cross_promote"] is True
    assert ca.settings_public({"cross_promote": False})["cross_promote"] is False


def test_validate_settings_parses_cross_promote():
    out, errors = ca.validate_settings({"cross_promote": False})
    assert errors == []
    assert out["cross_promote"] is False
