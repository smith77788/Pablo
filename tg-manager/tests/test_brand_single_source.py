"""Регресс: бренд-хендл в постах канала берётся из единого источника.

`botmother_channel` писал `@MEXAHI3MBOT` литералом в 9 промо/чейнджлог-текстах,
минуя канонический `brand_injection.PROMO_USERNAME`. При смене хендла публичные
посты канала уводили бы подписчиков на МЁРТВЫЙ юзернейм, тогда как контент,
генерируемый brand_injection, обновлялся бы корректно — классический «второй
источник правды» (класс 2 СВОД).

Фикс — подстановка в единой воронке публикации `post()`, по образцу уже
существующего `_apply_price` (цена тоже подставляется из конфига).
"""
from __future__ import annotations

import inspect

from services import botmother_channel as bm


def test_post_applies_brand_from_single_source():
    src = inspect.getsource(bm.post)
    assert "_apply_brand(text)" in src, (
        "единая воронка post() обязана подставлять канонический бренд-хендл"
    )


def test_apply_brand_reads_canonical_constant():
    src = inspect.getsource(bm._apply_brand)
    assert "PROMO_USERNAME" in src, "источник правды — brand_injection.PROMO_USERNAME"


def test_brand_substituted_when_handle_changes(monkeypatch):
    from services import brand_injection
    monkeypatch.setattr(brand_injection, "PROMO_USERNAME", "NEWBRANDBOT")
    out = bm._apply_brand("Подписка 💎\n👉 @MEXAHI3MBOT")
    assert "@NEWBRANDBOT" in out, "при смене хендла посты должны вести на новый бренд"
    assert "MEXAHI3MBOT" not in out.replace("NEWBRANDBOT", ""), "старый хендл не должен остаться"


def test_no_substitution_when_handle_unchanged():
    # текущий хендл совпадает с шаблоном → текст остаётся байт-в-байт
    text = "Подписка 💎\n👉 @MEXAHI3MBOT"
    assert bm._apply_brand(text) == text


def test_apply_brand_fail_open(monkeypatch):
    # сбой импорта/константы не должен ронять публикацию
    import services.brand_injection as bi
    monkeypatch.delattr(bi, "PROMO_USERNAME", raising=False)
    text = "текст 👉 @MEXAHI3MBOT"
    assert bm._apply_brand(text) == text
