"""Поиск по нику: скопировать все найденные ссылки и повторить поиск.

Найденные профили открывались только по одному, а повторить старый поиск
можно было, лишь вспомнив и набрав ник заново. Гоняем настоящие функции из
screens/osint_search.js в node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "osint_search.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _node(body):
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = ("let _osLinks = [], _osLastQuery = ''; let copied = null, backs = 0;\n"
          "const inp = {value: '', focus() {}}; const document = {getElementById: () => inp};\n"
          "function copyToClipboard(t, m) { copied = [t, m]; } function back() { backs++; }\n"
          + _fn("_osQueryOf") + _fn("osintCopyLinks") + _fn("osintRepeat") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_query_is_taken_from_the_label():
    got = _node("console.log(JSON.stringify([_osQueryOf('Поиск по нику: johndoe'),"
                " _osQueryOf('Поиск по почте: a@b.ru'), _osQueryOf('Поиск'), _osQueryOf(null)]));")
    assert got == ["johndoe", "a@b.ru", "", ""]


def test_copy_puts_every_link_on_its_own_line():
    got = _node("_osLinks = ['https://a.io/x', 'https://b.io/x']; osintCopyLinks();"
                " console.log(JSON.stringify(copied));")
    assert got == ["https://a.io/x\nhttps://b.io/x", "🔗 Скопировано ссылок: 2"]


def test_repeat_returns_to_search_with_the_query_filled():
    got = _node("_osLastQuery = 'johndoe'; osintRepeat();"
                " console.log(JSON.stringify({v: inp.value, backs}));")
    assert got == {"v": "johndoe", "backs": 1}


def test_result_screen_has_the_buttons():
    assert 'onclick="osintCopyLinks()"' in JS and 'onclick="osintRepeat()"' in JS
