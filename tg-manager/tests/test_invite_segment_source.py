"""Инвайтинг: сегмент контактов как источник аудитории (единый движок)."""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api():
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _func_body(src: str, name: str) -> str:
    """Тело функции по имени — границы из AST, а не окно фиксированной длины.

    Окно в N символов промахивается, стоит вставить комментарий или строку: код
    сдвинулся — «искомого нет» стало правдой — проверка молча позеленела. Берём
    ровно тело функции.
    """
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"{name} не найдена в mini_app_api.py")


def test_submit_accepts_segment_source():
    src = _api()
    assert '"parsed", "crm", "bot_users", "import_list", "segment"' in src
    body = _func_body(src, "mass_inviter_submit")
    assert "saved_segment_id" in body and "segment_filters" in body


def test_audience_size_supports_segment():
    src = _api()
    assert '"parsed", "crm", "bot_users", "segment"' in src
    body = _func_body(src, "invite_audience_size")
    assert "count_segment" in body and "get_segment_filters" in body


def test_executor_resolves_segment_via_engine():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = ow.index('elif source == "segment":')
    fn = ow[i:i + 900]
    # единый движок сегментов, а не legacy crm_contacts
    assert "resolve_segment" in fn
    assert "get_segment_filters" in fn
    assert "unified" in fn.lower() or "repository" in fn


def test_frontend_segment_source_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'value="segment"' in html
    assert "massInviteSegmentField" in html
    assert "function loadInviteSegments" in html
    assert "body.saved_segment_id" in html
    assert "/api/miniapp/uch/segments" in html


def test_frontend_parse_run_picker_unifies_sources():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    # прошлые запуски парсера (конкуренты/поиск) выбираются в шаге источника
    assert "massInviteParseRunField" in html
    assert "function loadInviteParseRuns" in html
    assert "function selectInviteParseRun" in html
    assert "/api/miniapp/parser/runs" in html
