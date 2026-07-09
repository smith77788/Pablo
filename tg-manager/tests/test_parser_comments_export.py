"""Регрессия: парсинг комментаторов + многоформатный экспорт аудитории.

Чистые хелперы экспорта тестируем напрямую; parse_commenters (сеть/Telethon) —
через source-level guard, что тип 'comments' подключён в диспетчере op_worker и
принят эндпоинтом сабмита (иначе кнопка UI — мёртвая).
"""
from __future__ import annotations

import json
import os

from services.parser import audience_to_txt, audience_to_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_txt_prefers_username_then_id():
    users = [
        {"username": "alice", "tg_user_id": 1},
        {"username": "", "tg_user_id": 2},
        {"username": None, "tg_user_id": 3},
    ]
    assert audience_to_txt(users).splitlines() == ["@alice", "2", "3"]


def test_txt_empty():
    assert audience_to_txt([]) == ""


def test_json_roundtrips_and_is_list():
    users = [{"tg_user_id": 5, "username": "bob", "is_premium": True}]
    parsed = json.loads(audience_to_json(users))
    assert isinstance(parsed, list) and parsed[0]["username"] == "bob"
    assert parsed[0]["is_premium"] is True


def test_comments_wired_in_op_worker():
    src = _read("services/op_worker.py")
    assert 'parse_type == "comments"' in src
    assert "parse_commenters(" in src, "comments не диспетчеризуется в op_worker"


def test_comments_accepted_by_submit_endpoint():
    src = _read("services/mini_app_api.py")
    assert '("members", "active", "comments")' in src, "эндпоинт сабмита не принимает comments"


def test_export_supports_multiformat():
    src = _read("services/mini_app_api.py")
    # эндпоинт экспорта различает форматы и зовёт чистые хелперы parser
    assert '("csv", "txt", "json")' in src
    assert "audience_to_txt(users)" in src and "audience_to_json(users)" in src


def test_no_duplicate_filter_helper_in_parser():
    # build_audience_filters дублировал mini_app_api.parsed_audience_filters — удалён
    src = _read("services/parser.py")
    assert "def build_audience_filters" not in src, "дубль фильтра аудитории вернулся в parser"
