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


def test_ecosystem_members_queries_use_owner_id():
    src = inspect.getsource(mini_app_api)
    matches = re.findall(r"ecosystem_id FROM ecosystem_members WHERE owner_id=\$\d", src)
    assert len(matches) >= 5, (
        "ожидались запросы к ecosystem_members, скоупленные по owner_id"
    )


def test_db_module_has_no_ecosystem_members_user_id():
    """database/db.py тоже фильтровал ecosystem_members по user_id (проверка
    владения ботом через экосистему) — тот же баг, что в mini_app_api."""
    from database import db as _db

    src = inspect.getsource(_db)
    bad = re.findall(r"ecosystem_members WHERE user_id=", src)
    assert not bad, (
        "database/db.py: ecosystem_members не имеет колонки user_id (только owner_id)"
    )

