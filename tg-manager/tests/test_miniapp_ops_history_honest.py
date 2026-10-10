"""История операций: показано столько, сколько сказано, и есть чем листать.

Разрыв, который это закрывает. `/api/miniapp/operations` был жёстко
`LIMIT 30` — без offset и без общего числа. Истории дальше тридцати последних
операций в продукте просто не существовало: всё, что случилось раньше, до
интерфейса не доходило никак.

Хуже того, плитки над списком («Всего», «Готово», «Работают», «Ожидают»,
«Ошибки») считались ПО ЭТИМ ТРИДЦАТИ строкам. У владельца с двумя сотнями
операций экран уверенно показывал «Всего 30», а «Ошибки 2» означало «две ошибки
среди тридцати последних», а не в очереди. Обе цифры были следствием размера
страницы, а не состояния системы — ровно то, что стандарт называет
«analytics без достоверного источника».
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.miniapp_source import miniapp_html, source_of

ROOT = Path(__file__).resolve().parents[1]


def _operations_handler() -> str:
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "operations":
            lines = src.splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("обработчик operations не найден")


def test_operations_endpoint_pages_and_counts():
    body = _operations_handler()
    assert "OFFSET" in body, "список операций без offset — истории дальше страницы нет"
    assert "LIMIT $" in body, "размер страницы должен быть параметром, а не константой"
    # Комментарии рассказывают, КАК было, и «LIMIT 30» в них законен — смотрим
    # на код.
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in body.splitlines())
    assert re.search(r"LIMIT\s+30", code) is None, (
        "жёсткий LIMIT 30 вернулся — это и был потолок всей истории")
    assert '"counts"' in body and "GROUP BY status" in body, (
        "счётчики по статусам должны считаться по всей очереди, а не по странице")
    assert '"total"' in body


def test_counters_come_from_server_not_from_the_page():
    """Плитки не должны пересчитываться по загруженным строкам как «всего»."""
    src = source_of("loadOps")
    m = re.search(r"^async function loadOps\s*\(", src, re.M)
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = src[i:j + 1]
    assert "d.counts" in body, "плитки снова считаются по странице, а не по очереди"
    # Когда сервер счётчики не прислал, подпись обязана перестать обещать «Всего».
    assert "'Всего':'Показано'" in body or '"Всего":"Показано"' in body, (
        "без серверных счётчиков плитка обязана называться «Показано», "
        "иначе размер страницы выдаётся за размер очереди")


def test_ops_screen_has_load_more():
    html = miniapp_html()
    assert 'id="opsLoadMore"' in html and "loadMoreOps()" in html


def test_changing_filter_starts_a_new_page():
    src = source_of("filterOps")
    i = src.index("function filterOps(")
    body = src[i:i + 700]
    assert "OPS_OFFSET = 0" in body, (
        "смена среза не сбрасывает страницу — остаток считался бы от прошлого фильтра")
