"""Регрессия: разбор своего списка для инвайтера + парсеры движка.

import_list как источник инвайта раньше делил токены строго по префиксу «+»:
всё без «+» уходило в user_refs. Из-за этого вставленный список НОМЕРОВ без «+»
(79991234567) трактовался как Telegram ID → инвайт добавлял «0 из 0».

Теперь классификация в едином `split_invite_targets`: «+» ИЛИ 11+ цифр → телефон;
короткий числовой токен (≤10 цифр) остаётся ID (нет двойного инвайта).
"""
from __future__ import annotations

from services.mass_inviter_engine import (
    split_invite_targets,
    parse_user_refs,
    parse_phones,
)


def test_numeric_id_is_ref_not_phone():
    # ≤10 цифр — это ID, не телефон (иначе двойной инвайт).
    refs, phones = split_invite_targets("123456789")
    assert refs == ["123456789"]
    assert phones == []


def test_bare_11digit_number_is_phone():
    # РЕГРЕССИЯ бага: номер без «+» из 11 цифр должен уйти в телефоны, а не в refs.
    refs, phones = split_invite_targets("79111491199")
    assert phones == ["+79111491199"]
    assert refs == []


def test_pasted_phone_list_without_plus():
    # Ровно кейс из отчёта: пять RU-номеров без «+», по одному на строку.
    raw = "79111491199\n79265285721\n79162678898\n79154547587\n79261234342"
    refs, phones = split_invite_targets(raw)
    assert refs == []
    assert phones == [
        "+79111491199",
        "+79265285721",
        "+79162678898",
        "+79154547587",
        "+79261234342",
    ]


def test_plus_is_phone_only():
    refs, phones = split_invite_targets("+79991234567")
    assert phones == ["+79991234567"]
    assert refs == []


def test_mixed_list_no_overlap():
    refs, phones = split_invite_targets("@alice\n123456789\n+79990001122\n79991112233\n@bob")
    assert "@alice" in refs and "@bob" in refs and "123456789" in refs
    assert phones == ["+79990001122", "+79991112233"]
    # ни один элемент не задублирован между refs и phones
    assert not (set(refs) & set(phones))


def test_username_normalization():
    refs, _ = split_invite_targets("alice, @bob")
    assert refs == ["@alice", "@bob"]


def test_empty():
    refs, phones = split_invite_targets("   \n  ")
    assert refs == [] and phones == []


def test_engine_parsers_still_exposed():
    # split_invite_targets переиспользует движковые парсеры — они остаются публичными.
    assert parse_user_refs("@x_test") == ["@x_test"]
    assert parse_phones("+79991234567") == ["+79991234567"]
