"""Массовые действия по контактам: сервер умел больше, чем показывала панель.

На панели выбора было пять кнопок, а `bulk_ops_engine` умеет ещё снять тег,
выставить важность и оценку, убрать из группы — четыре маршрута, которые с
фронта не звал никто.

Заодно закрыты три бага, проверенные на живом Postgres (28.09.2026):
* `bulk_untag` обновляла контакт независимо от того, был ли тег, и рапортовала
  «снято у N» всегда — счётчик врал, а «тега ни у кого не было» не показывалось
  никогда;
* пустой список контактов давал `id IN ()` — синтаксическую ошибку базы вместо
  честного «ничего не выбрано»;
* `user_rating` — INTEGER, а движок принимал float: дробное значение asyncpg
  отверг бы.

И отдельная мина на экране выгрузки: три ссылки уходили новой вкладкой БЕЗ
заголовка Authorization и возвращали 401. Токен в query — тот же способ, что
у выгрузки разобранной аудитории.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
ENGINE = os.path.join(ROOT, "services", "contacts_hub", "bulk_ops_engine.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


@functools.lru_cache(maxsize=1)
def _engine_src() -> str:
    with open(ENGINE, encoding="utf-8") as f:
        return f.read()


def _py_func(name: str) -> str:
    src = _engine_src()
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def _arg_count(src: str, open_at: int) -> int:
    """Сколько аргументов у вызова — по балансу скобок, а не по запятым:
    внутри аргументов свои скобки (`Number(r.updated||0)`)."""
    depth = 0
    args = 1
    for j in range(open_at, len(src)):
        c = src[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return args
        elif c == "," and depth == 1:
            args += 1
    raise AssertionError("вызов не закрыт")


def test_panel_offers_the_rest():
    assert 'onclick="bulkMoreSelected()"' in _html(), "кнопки «Ещё» на панели нет"
    body = _js_func("bulkMoreSelected")
    for key in ("untag", "imp", "rating", "ungroup"):
        assert f"'{key}'" in body, f"в меню нет действия {key}"
    assert "askChoice" in body, "меню действий не из общего диалога выбора"


def test_each_action_calls_its_route():
    routes = {
        "bulkUntagSelected": "/api/miniapp/uch/bulk/untag",
        "bulkImportanceSelected": "/api/miniapp/uch/bulk/importance",
        "bulkRatingSelected": "/api/miniapp/uch/bulk/rating",
        "bulkUngroupSelected": "/api/miniapp/uch/bulk/group",
    }
    for fn, route in routes.items():
        body = _js_func(fn)
        assert route in body, f"{fn} не зовёт {route}"
        assert "contact_ids" in body, f"{fn} не передаёт выбранные контакты"


def test_zero_result_is_spoken_aloud():
    """Ноль изменённых — не ошибка, но и не молчание: кнопка выглядит нерабочей."""
    done = _js_func("_bulkDone")
    assert "nothingMsg" in done and "changed ?" in done.replace("  ", " ")
    for fn in ("bulkUntagSelected", "bulkImportanceSelected",
               "bulkRatingSelected", "bulkUngroupSelected"):
        body = _js_func(fn)
        i = body.find("_bulkDone(")
        assert i > 0, f"{fn} не сообщает итог"
        assert _arg_count(body, i + len("_bulkDone")) == 3, (
            f"{fn} не объясняет нулевой итог третьим аргументом")


def test_untag_counts_only_those_who_had_the_tag():
    body = _py_func("bulk_untag")
    assert "$3 = ANY(tags)" in body and "NOT ($3 = ANY(tags))" not in body, (
        "снятие тега по-прежнему трогает всех подряд")
    assert "RETURNING 1" in body and "len(rows)" in body, "счётчик считает не строки"


def test_empty_selection_does_not_reach_sql():
    for name in ("bulk_set_importance", "bulk_set_rating", "bulk_set_favorite",
                 "bulk_tag", "bulk_untag"):
        body = _py_func(name)
        assert "if not contact_ids" in body, (
            f"{name} на пустом списке соберёт «id IN ()» и уронит запрос")


def test_rating_is_an_integer():
    """user_rating — INTEGER; дробное значение asyncpg отвергнет."""
    body = _py_func("bulk_set_rating")
    assert re.search(r"rating\s*=\s*int\(", body), (
        "оценка уходит в запрос как есть — дробную asyncpg отвергнет")


def test_export_links_carry_the_token():
    h = _html()
    assert 'href="/api/miniapp/uch/export/csv"' not in h, (
        "ссылка выгрузки без токена вернётся 401 в новой вкладке")
    body = _js_func("uchExport")
    assert "token=" in body and "encodeURIComponent(TK)" in body
    assert "openLink" in body, "выгрузка должна открываться тем же способом, что и остальные файлы"
