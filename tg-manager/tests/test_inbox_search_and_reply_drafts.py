"""Входящие: поиск по разговорам и черновик ответа у каждого разговора.

Длинный список разговоров листался глазами — поиска не было. А недописанный
ответ пропадал при переходе к соседнему разговору или выходе с экрана.
Гоняем настоящие функции из screens/crm_inbox.js в node с поддельным DOM.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "crm_inbox.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _node(body):
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = ("let _ibQuery = '', _ibPeer = null; const drafts = {};\n"
          "function saveDraft(k, v) { if (v) drafts[k] = v; else delete drafts[k]; }\n"
          + _fn("_ibDraftKey") + _fn("_ibKeepDraft") + _fn("inboxFilter") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


ROWS = [{"name": "Иван Петров", "username": "ivanp", "last_text": "Сколько стоит?"},
        {"name": "Мария", "username": None, "last_text": "Спасибо, оплатила"},
        {"name": "", "username": "shop_bot_fan", "last_text": "привет"}]

DOM = ("const rows = _ibRows.map((t, i) => ({dataset: {i: String(i)}, style: {display: ''}}));\n"
       "let note = null; const list = {appendChild: n => { note = n; }};\n"
       "const document = {querySelectorAll: () => rows, querySelector: () => list,\n"
       "  getElementById: id => (id === 'ibSearchNone' ? note : null),\n"
       "  createElement: () => ({style: {}})};\n"
       "const vis = () => rows.map(r => r.style.display !== 'none');\n")


def _filter(q):
    return _node("const _ibRows = " + json.dumps(ROWS) + ";\n" + DOM +
                 "const n = inboxFilter(" + json.dumps(q) + ");\n"
                 "console.log(JSON.stringify({n, vis: vis(), none: note && note.style.display}));")


def test_search_matches_name_nick_and_text_case_insensitively():
    assert _filter("иван")["vis"] == [True, False, False]
    assert _filter("@SHOP")["vis"] == [False, False, True]
    assert _filter("оплатила")["vis"] == [False, True, False]
    got = _filter("")
    assert got["vis"] == [True, True, True] and got["none"] == "none"


def test_search_says_when_nothing_matches():
    got = _filter("zzz")
    assert got["n"] == 0 and got["none"] == ""


def test_reply_draft_is_kept_per_conversation():
    got = _node("const ta = {value: 'Добрый день, '};\n"
                "function _ibEl() { return ta; }\n"
                "_ibPeer = {peer_user_id: 42}; _ibKeepDraft();\n"
                "_ibPeer = {peer_user_id: 7}; ta.value = 'другому'; _ibKeepDraft();\n"
                "ta.value = ''; _ibKeepDraft();\n"
                "console.log(JSON.stringify(drafts));")
    assert got == {"inbox_42": "Добрый день, "}


def test_screen_wires_search_and_drafts():
    assert 'oninput="inboxFilter(this.value)"' in JS
    assert "loadDraft(_ibDraftKey(t))" in JS
    assert "clearDraft(_ibDraftKey(t))" in JS
    assert JS.count("_ibKeepDraft();") >= 2   # шаблон и ИИ-черновик тоже сохраняются
