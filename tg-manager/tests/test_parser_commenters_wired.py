"""Регрессия: бот-парсер не подключал parse_commenters (раздел 5 «Сбор аудитории»).

parse_commenters (авторы комментариев в обсуждениях канала) есть в движке и в
mini-app, но бот-меню предлагало только участников/активных, а _start_parse
диспетчил только members/active. Комментаторы через бота были недоступны.
"""
from __future__ import annotations

import inspect

from bot.handlers import audience_parser as ap
from services import parser


def test_engine_has_parse_commenters():
    assert hasattr(parser, "parse_commenters")


def test_menu_offers_commenters():
    src = inspect.getsource(ap._menu_kb)
    assert "start_comments" in src


def test_start_filter_includes_commenters():
    src = inspect.getsource(ap.cb_parser_start)
    assert "start_comments" in src and '"comments"' in src


def test_start_parse_dispatches_commenters():
    src = inspect.getsource(ap._start_parse)
    assert "parse_commenters" in src
    # диспетч по типу — comments должен вызывать именно parse_commenters
    assert 'parse_type == "comments"' in src
