"""Последние запросы и подсветка в поиске; лента — по дням.

Три мелочи, которые на телефоне стоят дорого:

* искать приходится одно и то же — «тот номер», «тот канал», — и набирать
  это заново с телефона дороже, чем кажется;
* в списке находок не видно, ЧЕМ строка совпала с запросом (совпасть могло
  поле, которого на экране нет, — например op_type у операции);
* лента уведомлений без разделителей по дням — стена из тридцати строк
  «2 часа назад / вчера / 12 ноября».
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FA = (ROOT / "mini_app" / "screens" / "find_all.js").read_text(encoding="utf-8")
NF = (ROOT / "mini_app" / "screens" / "notifications_feed.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")

_ESC = """
function esc(s) { return String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
"""


def _fn(src: str, name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


def _run(src: str):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_highlighting_escapes_first_and_survives_special_characters():
    out = _run(_ESC + _fn(FA, "faMark") + r"""
console.log(JSON.stringify({
  plain: faMark('Вега Иванова', 'вега'),
  tags: faMark('Скидка <b>30%</b>', '<b>'),
  percent: faMark('100% скидка', '100%'),
  dot: faMark('a.b', '.'),
  none: faMark('Вега', ''),
}));
""")
    assert out["plain"].startswith('<mark class="fa-hit">Вега</mark>')
    assert "&lt;b&gt;" in out["tags"], "разметка владельца обязана остаться текстом"
    assert out["percent"].startswith('<mark class="fa-hit">100%</mark>')
    assert out["dot"] == 'a<mark class="fa-hit">.</mark>b', (
        "точка в запросе — это точка, а не «любой символ»")
    assert out["none"] == "Вега"
    assert ".fa-hit{" in HTML, "у подсветки нет стиля"


def test_the_history_keeps_only_what_was_found():
    out = _run("""
let _LS = {};
const localStorage = { getItem: k => _LS[k] == null ? null : _LS[k],
                       setItem: (k, v) => { _LS[k] = String(v); },
                       removeItem: k => { delete _LS[k]; } };
const FA_HISTORY_KEY = 'k';
const FA_HISTORY_MAX = 8;
""" + _fn(FA, "faHistory") + _fn(FA, "faRemember") + r"""
for (const q of ['вега', 'прокси', 'вега', '+7999']) faRemember(q);
faRemember('я');                       // короткий запрос не помним
const after = faHistory();
for (let i = 0; i < 12; i++) faRemember('q' + i);
_LS[FA_HISTORY_KEY] = 'не json';       // испорченное хранилище не ломает экран
console.log(JSON.stringify({after, capped: (JSON.parse('[]'), 0), n: faHistory().length}));
""")
    assert out["after"][0] == "+7999"
    assert out["after"].count("вега") == 1, "повтор не плодит вторую строку"
    assert "я" not in out["after"]
    assert out["n"] == 0, "испорченное хранилище — пустая история, а не поломка"


def test_the_feed_is_split_by_calendar_days():
    out = _run(_fn(NF, "nfDayLabel") + r"""
const now = new Date();
const d = (h) => new Date(now.getTime() - h * 3600e3).toISOString();
console.log(JSON.stringify({
  today: nfDayLabel(now.toISOString()),
  yesterday: nfDayLabel(d(26)),
  old: nfDayLabel('2026-11-12T10:00:00Z'),
  junk: nfDayLabel('мусор'),
  none: nfDayLabel(null),
}));
""")
    assert out["today"] == "Сегодня" and out["yesterday"] == "Вчера"
    assert "ноября" in out["old"]
    assert out["junk"] == "" and out["none"] == ""
    assert "nfDayLabel(it.created_at)" in NF, "лента не разделена по дням"


def test_the_search_history_has_a_way_in_and_out():
    assert "faForget()" in FA and "Последние запросы" in FA
    assert "faRemember(q)" in FA
