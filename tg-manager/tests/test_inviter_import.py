"""Регрессия: разбор своего списка для инвайтера + парсеры движка.

import_list как источник инвайта раньше не поддерживался (UI давал 3 таблицы).
Ключевой риск: числовой ID мог попасть и в user_refs, и в phones (двойной инвайт) —
эндпоинт делает разбор взаимоисключающим (телефон только с '+').
"""
from __future__ import annotations

import re

from services.mass_inviter_engine import parse_user_refs, parse_phones


def _split_import(raw: str):
    """Повторяет логику эндпоинта: '+' → телефон, остальное → ref."""
    tokens = [t for t in re.split(r"[,;\s\n]+", raw.strip()) if t]
    phones = parse_phones(" ".join(t for t in tokens if t.startswith("+")))
    refs = parse_user_refs(" ".join(t for t in tokens if not t.startswith("+")))
    return refs, phones


def test_numeric_id_is_ref_not_phone():
    refs, phones = _split_import("123456789")
    assert refs == ["123456789"]
    assert phones == []  # без '+' не считается телефоном → нет двойного инвайта


def test_plus_is_phone_only():
    refs, phones = _split_import("+79991234567")
    assert phones == ["+79991234567"]
    assert refs == []


def test_mixed_list_no_overlap():
    refs, phones = _split_import("@alice\n123456789\n+79990001122\n@bob")
    assert "@alice" in refs and "@bob" in refs and "123456789" in refs
    assert phones == ["+79990001122"]
    # ни один элемент не задублирован между refs и phones
    assert not (set(refs) & set(phones))


def test_username_normalization():
    refs, _ = _split_import("alice, @bob")
    assert refs == ["@alice", "@bob"]


def test_empty():
    refs, phones = _split_import("   \n  ")
    assert refs == [] and phones == []
