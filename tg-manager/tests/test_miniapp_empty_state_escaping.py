"""Регрессия: пустые состояния и ошибки не вставляют текст как разметку.

`empty(ico, txt2, sub)` собирает блок «Нет данных / Ошибка» и кладёт результат
в innerHTML. Текст в него приходит снаружи:

  * `empty('⚠️','Ошибка', e.message)` — 73 вызова, где `e.message` это текст
    ответа сервера, а сервер в отказах 4xx возвращает обратно то, что прислал
    клиент («Неизвестный статус операции: …», «Неизвестный тип операции: …»);
  * подписи с именами каналов, контактов и аккаунтов, то есть содержимым из
    Telegram, которое по правилу проекта является данными, а не разметкой.

Соседний `errHtml()` звал `esc()`, а `empty()` — нет, и строка попадала в
innerHTML как HTML. Ни один из 252 вызовов не передаёт разметку намеренно,
поэтому экранирование ничего не ломает.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _html() -> str:
    return INDEX.read_text("utf-8")


def _fn_source(name: str) -> str:
    """Тело функции верхнего уровня из index.html."""
    src = _html()
    start = src.index(f"\nfunction {name}(")
    end = src.index("\n}", start) + 2
    return src[start:end]


def test_empty_escapes_text_and_sub():
    body = _fn_source("empty")
    assert "${esc(txt2)}" in body, "заголовок пустого состояния не экранируется"
    assert "${esc(sub)}" in body, "пояснение пустого состояния не экранируется"


def test_empty_escapes_action_label():
    body = _fn_source("empty")
    assert "${esc(action.label)}" in body


def test_err_html_still_escapes():
    """Паритет с соседним хелпером — он и был образцом."""
    assert "${esc(msg)}" in _fn_source("errHtml")


def _empty_call_args(src: str) -> list[str]:
    """Аргументы каждого вызова empty(...) — со сбалансированными скобками."""
    out: list[str] = []
    for m in re.finditer(r"(?<![\w.])empty\(", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            ch = src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        out.append(src[m.end():i - 1])
    return out


def test_no_caller_passes_markup_to_empty():
    """Экранирование безопасно ровно потому, что разметку никто не передаёт."""
    src = _html()
    for js in sorted((INDEX.parent / "screens").glob("*.js")):
        src += js.read_text("utf-8")
    bad = [a for a in _empty_call_args(src)
           if re.search(r"<(b|i|u|a|br|hr|span|div|p|strong|em|code|img|script)\b", a)]
    assert not bad, (
        "вызов empty() передаёт разметку — экранирование её сломает:\n  "
        + "\n  ".join(a[:90] for a in bad[:10]))


# ── Поведение: прогоняем сами функции в node ─────────────────────────────────

_RUNNER = """
%(esc)s
%(empty)s
const STACK = [];
function reloadView() {}
function back() {}
const out = [];
out.push(empty('\\u26a0\\ufe0f', 'Ошибка', "<script>alert(1)</script>"));
out.push(empty('\\u2699\\ufe0f', 'Нет операций', 'По фильтру «<img src=x onerror=alert(1)>»'));
out.push(empty('\\u2699\\ufe0f', '<b>жирный</b>', null));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_empty_renders_payload_as_text_not_markup():
    script = _RUNNER % {"esc": _fn_source("esc"), "empty": _fn_source("empty")}
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    rendered = json.loads(res.stdout)

    for html in rendered:
        # Угловые скобки закрыты — значит ни тег, ни атрибут-обработчик браузер
        # не разберёт: всё это остаётся видимым текстом внутри <div>.
        inner = html[html.index('class="empty-txt"'):]
        assert "<script" not in inner and "<img" not in inner, (
            "полезная нагрузка отрендерена как разметка")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered[0], (
        "текст ошибки должен быть виден пользователем как текст")
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered[1]
    assert "&lt;b&gt;жирный&lt;/b&gt;" in rendered[2]
    # Эмодзи-иконка остаётся как есть — она задаётся в коде, не снаружи.
    assert "⚠" in rendered[0]
