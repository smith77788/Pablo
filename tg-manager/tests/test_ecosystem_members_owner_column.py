"""Регрессия: несколько запросов в mini_app_api.py фильтровали
`ecosystem_members` по несуществующей колонке `user_id`, что валило запрос с
`column "user_id" does not exist` (реальная колонка — owner_id, см.
schema_v67.sql). Ломало разделы бот-статистики/каналов, которые проверяют
членство пользователя в экосистеме.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def test_no_ecosystem_members_queries_use_user_id():
    src = inspect.getsource(mini_app_api)
    bad = re.findall(r"ecosystem_members WHERE user_id=", src)
    assert not bad, (
        "ecosystem_members не имеет колонки user_id (только owner_id) — "
        "найдены запросы, использующие несуществующую колонку"
    )


def test_membership_lookups_are_scoped_by_owner_id():
    """Проверка «в какие экосистемы входит пользователь» идёт по owner_id.

    Раньше здесь стояло «таких запросов не меньше пяти». Это требование не к
    корректности, а к ДУБЛИРОВАНИЮ: как только четыре дословные копии проверки
    доступа свели в один помощник (`_user_can_use_bot`), счётчик упал до двух и
    тест покраснел на правильной правке. Проверяем настоящий инвариант: выборка
    «мои экосистемы» по-прежнему скоупится по owner_id и не исчезла совсем.
    """
    src = inspect.getsource(mini_app_api)
    scoped = re.findall(r"ecosystem_id FROM ecosystem_members WHERE owner_id=\$\d", src)
    assert scoped, (
        "в mini_app_api не осталось ни одной выборки «мои экосистемы» по "
        "owner_id — либо её потеряли, либо переписали, и этот тест больше "
        "ничего не охраняет")
    unscoped = re.findall(r"ecosystem_id FROM ecosystem_members WHERE (?!owner_id)", src)
    assert not unscoped, (
        f"выборка экосистем пользователя без owner_id: {unscoped}")


def test_db_module_has_no_ecosystem_members_user_id():
    """database/db.py тоже фильтровал ecosystem_members по user_id (проверка
    владения ботом через экосистему) — тот же баг, что в mini_app_api."""
    from database import db as _db

    src = inspect.getsource(_db)
    bad = re.findall(r"ecosystem_members WHERE user_id=", src)
    assert not bad, (
        "database/db.py: ecosystem_members не имеет колонки user_id (только owner_id)"
    )

