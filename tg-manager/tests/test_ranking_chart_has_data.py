"""График позиций рисуется, а список ключей можно не только пополнять.

Экран «🏆 Рейтинг — позиции» строит график по `k.history`, а свод
`/api/miniapp/ranking` историю не отдавал вовсе. Фильтр
`k.history && k.history.length > 1` отсеивал ВСЕ ключи, холст прятался, и под
заголовком «График позиций» оставалась подпись «Добавьте ключевые слова» —
даже когда слов десяток и замеры идут месяцами.

Рядом две мелочи того же рода: снять слово с отслеживания было нечем (маршрут
`/ranking/untrack` с фронта не звал никто), а подпись под пустым графиком
валила всё на отсутствие слов, хотя причина бывает другой — замеров ещё мало.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
ENGINE = os.path.join(ROOT, "services", "ranking_engine.py")


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


def _py_func(path: str, name: str) -> str:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {path}")


def test_chart_reads_history_and_server_sends_it():
    """Антивакуумность: проверяем обе стороны контракта разом."""
    chart = _js_func("renderRankingChart")
    assert "k.history" in chart, "график перестал читать историю — проверка ниже пуста"

    overview = _py_func(API, "ranking_overview")
    assert '"history"' in overview, "свод не отдаёт историю, график не появится никогда"
    assert "get_history_for_all" in overview, "история берётся не одним запросом"


def test_history_is_one_query_not_a_loop():
    """На десятках ключей поштучные рейсы до базы экран бы не пережил."""
    body = _py_func(ENGINE, "get_history_for_all")
    assert body.count("await pool.fetch") == 1, "история тянется несколькими запросами"
    assert "ROW_NUMBER()" in body, "нет ограничения числа точек на ключ"
    assert "tk.owner_id = $1" in body, "история не заперта по владельцу"
    assert "ORDER BY keyword_id, checked_at" in body, (
        "точки должны идти от старой к новой — холст рисует их подряд")


def test_empty_chart_names_the_real_reason():
    chart = _js_func("renderRankingChart")
    assert "kw.length" in chart, "подпись не различает «нет слов» и «нет замеров»"
    assert "Замеров пока мало" in chart


def test_keyword_can_be_untracked():
    assert "untrackRankingKeyword" in _js_func("renderRankingList"), (
        "в строке слова нет способа снять его с отслеживания")
    body = _js_func("untrackRankingKeyword")
    assert "/api/miniapp/ranking/untrack" in body
    assert "keyword_id" in body, "маршрут ждёт keyword_id"
    assert "confirmDelete" in body, "слово снимается без подтверждения"


def test_screen_error_has_a_way_out():
    assert "errHtml(e.message, 'loadRanking()')" in _js_func("loadRanking")
