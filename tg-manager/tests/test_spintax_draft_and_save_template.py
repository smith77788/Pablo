"""Спинтакс: текст помнится между заходами, шаблон сохраняется в шаблоны.

Экран очищал поле при каждом открытии, а готовый шаблон можно было только
скопировать в буфер — закрыл экран, и шаблона нет. Гоняем настоящие функции
из screens/spintax.js в node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "spintax.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _node(body):
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = ("let SPIN_TEMPLATES = []; const drafts = {}; const els = {}; let calls = [], toasts = [], answer = null;\n"
          "function el(id) { return els[id] || (els[id] = {id, value: '', style: {}}); }\n"
          "const document = {getElementById: el};\n"
          "function push() {} function txt() {}\n"
          "function loadDraft(k) { return drafts[k] || ''; } function saveDraft(k, v) { drafts[k] = v; }\n"
          "async function askPrompt(t, o) { return answer === undefined ? o.value : answer; }\n"
          "async function api(u, o) { calls.push([u, JSON.parse(o.body)]); return {ok: true}; }\n"
          "function toast(t) { toasts.push(t); } function errRu(e, d) { return d; }\n"
          + _fn("openSpintax") + _fn("saveSpinTpl") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_text_survives_reopening():
    got = _node("openSpintax(); el('spinText').value = 'Привет, друзья'; el('spinText').oninput();\n"
                "el('spinText').value = ''; openSpintax();\n"
                "console.log(JSON.stringify(el('spinText').value));")
    assert got == "Привет, друзья"


def test_template_goes_to_saved_templates_with_chosen_name():
    got = _node("SPIN_TEMPLATES = ['{Привет|Здравствуйте}, друзья']; answer = 'Приветствие';\n"
                "saveSpinTpl(0).then(() => console.log(JSON.stringify({calls, toasts})));")
    assert got["calls"] == [["/api/miniapp/template",
                             {"name": "Приветствие", "text": "{Привет|Здравствуйте}, друзья"}]]
    assert got["toasts"] == ["💾 Сохранено в шаблоны"]


def test_cancel_saves_nothing():
    got = _node("SPIN_TEMPLATES = ['x']; answer = null;\n"
                "saveSpinTpl(0).then(() => console.log(JSON.stringify(calls)));")
    assert got == []


def test_each_variant_has_the_button():
    assert 'onclick="saveSpinTpl(${i})"' in JS
