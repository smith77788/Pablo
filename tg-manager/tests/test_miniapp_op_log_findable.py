"""Лог операции: видно, сколько его всего, и можно дойти до неудачных целей.

РАЗРЫВ. `/api/miniapp/operation/{id}/log` отдавал один кусок на `LIMIT 500` без
общего числа, а экран писал в заголовок длину ПОЛУЧЕННОГО куска. У операции на
2000 целей секция называлась «📋 Лог (500)» — и это выдавалось за весь лог.

Лог — единственное место, где после массовой операции видно, КТО именно не
прошёл и ПОЧЕМУ. Среза по статусу не было: чтобы найти 37 неудачных целей среди
двух тысяч строк, оставалось листать блок высотой 300 пикселей. Массовая
операция без разбора неудач — это «✅ Готово» и счётчик, которому нечем верить.

Статус в строках приходит от воркера по-английски, и его показывали как есть:
владелец видел «failed». Тип операции в шапке деталей печатался тем же
машинным ключом — «mass_invite».

ЗАМЕРЕНО ПОСЛЕ ПРАВКИ (Chromium, лог на 2000 строк, 37 неудачных): заголовок
«показано 200 из 2000», срезы «Все 2000 · 🟢 Успешно 1963 · 🔴 Ошибка 37»,
тап по «Ошибка» даёт 37 строк одним запросом, догрузка доводит общий список до
конца.
"""
from __future__ import annotations

import pathlib
import re

from tests.miniapp_source import miniapp_html


def _api_src() -> str:
    return (pathlib.Path(__file__).resolve().parents[1]
            / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _js_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\s*\(", src)
    assert m, f"{name} не найдена"
    j = src.index("{", m.start())
    depth, k, n = 0, j, len(src)
    while k < n:
        c = src[k]
        if c in "'\"`":
            q, k = c, k + 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "/" and k + 1 < n and src[k + 1] == "/":
            nl = src.find("\n", k)
            if nl == -1:
                break
            k = nl
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError(f"не нашли конец {name}")


def _py_fn(src: str, name: str) -> str:
    i = src.index("async def " + name + "(")
    indent = len(src[:i].split("\n")[-1])
    out = []
    for line in src[src.index("\n", i):].split("\n"):
        if line.strip() and (len(line) - len(line.lstrip())) <= indent and out:
            break
        out.append(line)
    return "\n".join(out)


def test_log_endpoint_pages_and_counts():
    body = _py_fn(_api_src(), "operation_log")
    assert 'request.query.get("limit"' in body and 'request.query.get("offset"' in body, (
        "лог нельзя листать — остаток за границей куска недостижим")
    assert '"total": total' in body, "нет общего числа строк лога"
    assert "GROUP BY status" in body, (
        "нет счётчиков по статусам — срезам нечего показать, а владельцу нечем "
        "понять, сколько целей не прошло")
    assert 'request.query.get("status")' in body, (
        "нельзя запросить только неудачные цели — ради них на лог и смотрят")


def test_status_filter_is_not_injectable():
    """Статус приходит из строки запроса и уходит в SQL — берём только известные."""
    body = _py_fn(_api_src(), "operation_log")
    assert 'if status not in (' in body, "статус не сверяется со списком известных"
    assert "lower(status)=$2" in body, "статус подставляется не параметром"


def test_header_names_the_whole_log_not_the_page():
    body = _js_fn(miniapp_html(), "renderOpLog")
    assert "OPLOG_TOTAL" in body, "заголовок считает по полученной странице"
    assert "из ${_opLogNum(OPLOG_TOTAL)}" in body


def test_counts_are_exact_so_two_slices_differ():
    """num() округляет: 1963 и 2000 дают «2.0K», и срезы на вид одинаковы."""
    html = miniapp_html()
    chips = _js_fn(html, "renderOpLogChips")
    assert "_opLogNum(n)" in chips and "num(n)" not in chips, (
        "счётчик среза округлён — «Все» и «Успешно» читаются одинаково, а это "
        "ровно та цифра, ради которой на чипы смотрят")
    assert "function _opLogNum" in html


def test_the_failure_slice_exists_and_loads_by_itself():
    html = miniapp_html()
    assert "function filterOpLog" in html and "function loadMoreOpLog" in html
    q = _js_fn(html, "_opLogQuery")
    assert "status=" in q, "срез не уходит на сервер — фильтровали бы по странице"
    f = _js_fn(html, "filterOpLog")
    assert "OPLOG_ROWS = []" in f, (
        "срез не перезапрашивает список и показывает то, что уже было загружено")


def test_nothing_english_reaches_the_owner():
    html = miniapp_html()
    assert "function _opLogStRu" in html, "статус строки лога не переводится"
    rows = _js_fn(html, "renderOpLog")
    assert "_opLogStRu(l.status)" in rows, "в строке лога печатается сырой статус"
    assert 'Тип</div><div class="row-val">${esc(o.op_type)}' not in html, (
        "тип операции печатается машинным ключом вроде mass_invite")
