"""Удержание: период помнится, строка «Откуда уходят» открывает чат.

Каждый заход сбрасывал период на 30 дней, а строки оттока были просто
текстом — до чата, где правится приветствие (/guard), приходилось искать
вручную. Гоняем настоящие функции из screens/retention.js в node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "retention.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _node(body):
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = (re.search(r"let _rtDays = 30;\nconst RT_PERIODS = .*\n", JS).group(0) +
          "const prefs = {}; let asked = [], opened = [], toasts = [];\n"
          "function loadFormPrefs(k) { return prefs[k] || {}; }\n"
          "function saveFormPrefs(k, o) { prefs[k] = Object.assign({}, prefs[k] || {}, o); }\n"
          "function _rtScreen() {} function push() {}\n"
          "const body = {innerHTML: ''}; const document = {getElementById: () => body};\n"
          "async function api(u) { asked.push(u); throw new Error('стоп'); }\n"
          "function errHtml() { return ''; } function errRu() { return ''; }\n"
          "const tg = {openTelegramLink: u => opened.push(u)}; function toast(t) { toasts.push(t); }\n"
          + _fn("openRetention") + _fn("rtChatUrl") + _fn("rtOpenChat") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_period_is_remembered_between_visits():
    got = _node("(async () => { await openRetention(90); await openRetention(); await openRetention(5);"
                " console.log(JSON.stringify(asked)); })();")
    assert got == ["/api/miniapp/invite/retention?days=90",
                   "/api/miniapp/invite/retention?days=90",
                   "/api/miniapp/invite/retention?days=30"]


def test_chat_row_opens_supergroup_and_explains_otherwise():
    got = _node("rtOpenChat('-1001234567890'); rtOpenChat('-42');"
                " console.log(JSON.stringify({opened, toasts}));")
    assert got["opened"] == ["https://t.me/c/1234567890/999999999"]
    assert len(got["toasts"]) == 1 and "не открыть" in got["toasts"][0]


def test_rows_are_tappable_and_entry_points_use_memory():
    assert "onclick=\"rtOpenChat(\\''" in JS
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "openRetention(30)" not in html
