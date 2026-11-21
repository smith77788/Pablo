"""Настройки: сброс запомненных значений форм и черновиков.

Формы помнят последний выбор, а черновики лежат на устройстве; вернуть
значения по умолчанию можно было только очисткой данных Telegram. Гоняем
настоящие функции из index.html в node с поддельным localStorage.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(sig):
    m = re.search(r"\n(?:async )?function " + sig + r"[\s\S]*?\n}\n", HTML)
    assert m, sig
    return m.group(0)


def _run(store, confirm):
    if not shutil.which("node"):
        pytest.skip("нет node")
    one_liners = "\n".join(line for line in HTML.splitlines()
                           if line.startswith(("const _FP_KEY", "function _fpAll(")))
    js = ("const _m = new Map(Object.entries(" + json.dumps(store) + "));\n"
          "const localStorage = { get length() { return _m.size; }, key: i => [..._m.keys()][i] ?? null,"
          " getItem: k => _m.has(k) ? _m.get(k) : null, setItem: (k, v) => _m.set(k, String(v)),"
          " removeItem: k => _m.delete(k) };\n"
          "const shown = {}; let toasts = [];\n"
          "function txt(id, h) { shown[id] = h; } function toast(t) { toasts.push(t); }\n"
          "async function askConfirm() { return " + ("true" if confirm else "false") + "; }\n"
          + one_liners + "\n" + _fn(r"formMemoryCounts\(\)") + _fn(r"renderFormMemoryStatus\(\)")
          + _fn(r"resetFormMemory\(\)")
          + "resetFormMemory().then(() => console.log(JSON.stringify({keys: [..._m.keys()], shown, toasts})));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


STORE = {"infragram_formprefs": json.dumps({"dash": {"m": 1}, "ghost": {"a": 1}}),
         "infragram_draft_post": "текст", "infragram_draft_dm": "привет", "other_key": "1"}


def test_reset_clears_prefs_and_drafts_but_nothing_else():
    got = _run(STORE, True)
    assert got["keys"] == ["other_key"]
    assert "Пусто" in got["shown"]["set-fp-status"]


def test_cancel_keeps_everything():
    got = _run(STORE, False)
    assert sorted(got["keys"]) == sorted(STORE)


def test_nothing_to_reset_says_so():
    got = _run({"other_key": "1"}, True)
    assert got["toasts"] == ["Сбрасывать нечего"]


def test_settings_show_the_control():
    assert 'onclick="resetFormMemory()"' in HTML
    assert 'id="set-fp-status"' in HTML
    assert "renderFormMemoryStatus()" in _fn(r"openSettings\(\)")
