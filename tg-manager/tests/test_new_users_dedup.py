"""«Новые пользователи» — один человек одна строка, без своих аккаунтов флота.

Владелец (18.11.2026): нажал «Лиды» на главной — показало весь флот с дублями.
Причина: лента подписчиков давала строку на КАЖДЫЙ бот (тот же человек десять
раз) и включала собственные аккаунты флота (они подписаны на боты доставки).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_new_users_query_dedups_and_excludes_fleet():
    src = _src("services/mini_app_api.py")
    fn = src[src.index("async def new_users("):]
    fn = fn[:fn.index("async def new_users_export(")]
    # Одна строка на человека (DISTINCT ON по пользователю) + счётчик ботов.
    # Форма запроса сменилась ради скорости (агрегат с JOIN обратно на
    # bot_users шёл двумя полными проходами по таблице); что ответ тот же
    # строка в строку — доказывает tests/test_new_users_feed_is_fast_postgres.py.
    assert "DISTINCT ON (bu.user_id)" in fn and "bot_count" in fn
    # Исключение собственных аккаунтов флота — того же владельца.
    assert "NOT EXISTS (SELECT 1 FROM tg_accounts ta" in fn
    assert "ta.owner_id = $1 AND ta.tg_user_id = bu.user_id" in fn
    # total тоже считает уникальных и без своих.
    assert "COUNT(DISTINCT bu.user_id)" in fn


def test_frontend_shows_bot_count():
    html = _src("mini_app/index.html")
    fn = html[html.index("async function openNewUsers("):]
    fn = fn[:fn.index("async function openNewUsersMore(") if "async function openNewUsersMore(" in fn else fn.index("\nlet _platUsersCache")]
    assert "bot_count" in fn and "через" in fn
