"""Баг-репорт: ответ доходит до того, кто отчёт прислал.

Разбор отчёта писали в `error_reports.notes`, и прочитать его отправителю
было негде: экран показывал статус и первые 120 символов СВОЕГО ЖЕ текста,
ни одна строка не нажималась, а колонка с ответом вообще не читалась
запросом. Получалось одностороннее окно: отчёт уходит и пропадает.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    m = re.search(r"\n    async def " + re.escape(name) + r"\(request", API)
    assert m, f"хендлер {name} не найден"
    nxt = API.find("\n    async def ", m.end())
    return API[m.start():nxt if nxt > 0 else len(API)]


def test_answer_is_read_and_returned():
    h = _handler("my_error_reports")
    assert "notes" in h, "колонка с ответом не читается"
    assert '"answer"' in h, "ответ не отдаётся экрану"
    assert "WHERE user_id=$1" in h, "чужие отчёты"


def test_own_text_is_returned_whole():
    """120 символов своего же описания — нечитаемый огрызок."""
    h = _handler("my_error_reports")
    assert '[:120]' not in h, h


def test_report_opens_and_shows_the_answer():
    f = _fn("loadErrReports")
    assert "errRepTap(" in f, "строка отчёта не нажимается"
    t = _fn("errRepTap")
    assert "r.answer" in t and "showInfo(" in t, t
    assert "Ответ" in t, t
    assert "есть ответ" in f, "по списку не видно, на какой отчёт ответили"


def test_empty_state_says_what_will_happen():
    f = _fn("loadErrReports")
    i = f.index("Отчётов пока нет")
    chunk = f[i:f.index("));", i) + 3]
    assert "ответ" in chunk, chunk


def test_statuses_are_russian():
    body = HTML[HTML.index("const ERR_REP_STATUS"):]
    body = body[:body.index("}")]
    for key in ("new", "viewing", "fixing", "fixed", "duplicate"):
        assert key + ":" in body, f"статус {key} без русской подписи"
    assert not re.search(r"'[A-Za-z ]{4,}'", body), body
