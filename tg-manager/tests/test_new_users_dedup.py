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
    # Группировка по пользователю (одна строка на человека) + счётчик ботов.
    assert "GROUP BY bu.user_id" in fn and "bot_count" in fn
    # Исключение собственных аккаунтов флота.
    assert "NOT IN (SELECT tg_user_id FROM own)" in fn
    assert "FROM tg_accounts" in fn and "owner_id=$1" in fn
    # total тоже считает уникальных и без своих.
    assert "COUNT(DISTINCT bu.user_id)" in fn


def test_frontend_shows_bot_count():
    html = _src("mini_app/index.html")
    fn = html[html.index("async function openNewUsers("):]
    fn = fn[:fn.index("async function openNewUsersMore(") if "async function openNewUsersMore(" in fn else fn.index("\nlet _platUsersCache")]
    assert "bot_count" in fn and "через" in fn
