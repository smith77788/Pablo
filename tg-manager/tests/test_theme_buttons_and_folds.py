"""Мелочи оформления, которые ломали тему и вид главного экрана.

* Кнопка без варианта (`class="btn"`) брала системный фон браузера: в тёмной
  теме серый #6b6b6b, красная «🚫 Забанить» на нём давала 1,6:1. Теперь фон и
  цвет текста у .btn по умолчанию из темы.
* У сворачиваемых блоков главного экрана свой шеврон, а общий треугольник
  details вставал над заголовком отдельной строкой.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html


def _rule(sel: str) -> str:
    m = re.search(r"(?:^|\})\s*" + re.escape(sel) + r"\{([^}]*)\}", miniapp_html(), re.MULTILINE)
    assert m, f"правило {sel} не найдено"
    return m.group(1)


def test_plain_button_takes_theme_colors():
    body = _rule(".btn")
    assert "background:var(--bg3)" in body and "color:var(--text)" in body


def test_ban_button_is_destructive_variant():
    assert '<button class="btn btn-d" onclick="admUserBan()">' in miniapp_html()


def test_home_fold_has_no_second_marker():
    assert "content:none" in _rule(".home-fold>summary::before")


def test_activity_badge_never_shows_a_raw_status_word():
    """Значок статуса в «Последних событиях» не выводит сырое слово сервера."""
    import json
    import shutil
    import subprocess

    from tests.miniapp_source import js_function

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("нужен node")
    prog = f"""
const els = {{}};
const document = {{ getElementById(id) {{ return els[id] || (els[id] = {{ innerHTML: '' }}); }} }};
function empty() {{ return ''; }} function esc(s) {{ return String(s); }} function ago() {{ return ''; }}
{js_function("renderAct")}
renderAct([{{ action: 'operation_done', status: 'active' }}, {{ action: 'operation_done', status: 'weird_new' }}]);
console.log(JSON.stringify(els.actList.innerHTML));
"""
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    html = json.loads(out.stdout.strip().splitlines()[-1])
    assert "active" not in html and "weird_new" not in html, html
