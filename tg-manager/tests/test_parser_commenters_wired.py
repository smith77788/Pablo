"""Регрессия: бот-парсер не подключал parse_commenters (раздел 5 «Сбор аудитории»).

parse_commenters (авторы комментариев в обсуждениях канала) есть в движке и в
mini-app, но бот-меню предлагало только участников/активных, а _start_parse
диспетчил только members/active. Комментаторы через бота были недоступны.
"""
from __future__ import annotations

import os

from services import parser


def test_engine_has_parse_commenters():
    assert hasattr(parser, "parse_commenters")


def test_menu_offers_commenters():
    path = os.path.join(os.path.dirname(__file__), "..", "bot", "handlers", "audience_parser.py")
    with open(path) as f:
        src = f.read()
    assert "start_comments" in src


def test_start_filter_includes_commenters():
    path = os.path.join(os.path.dirname(__file__), "..", "bot", "handlers", "audience_parser.py")
    with open(path) as f:
        src = f.read()
    assert "start_comments" in src and '"comments"' in src


def test_bus_executor_dispatches_commenters():
    """Бот больше не зовёт разбор сам — он ставит операцию в шину, а
    комментаторов разбирает исполнитель `parse_audience`."""
    path = os.path.join(os.path.dirname(__file__), "..", "services", "op_worker.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("async def _exec_parse_audience("):]
    body = body[:body.index("async def _exec_reg_check(")]
    assert 'parse_type == "comments"' in body
    assert "parse_commenters" in body
