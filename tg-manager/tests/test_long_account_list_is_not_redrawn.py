"""Подгрузка и выбор строки не перерисовывают весь список аккаунтов.

НАХОДКА (08.11.2026, замер Playwright на 4x-замедленном CPU). На мобильном
списке каждая подгрузка «ещё 100» строила HTML ВСЕХ загруженных строк заново:
0,3 с на первой странице и 4,5 с к трём тысячам строк. Каждое касание в режиме
выбора стоило столько же — `toggleAccSel` звал `renderAccounts` целиком. На
тысячах аккаунтов это и есть «зависает».

Правило: подгрузка дописывает только новую страницу (`insertAdjacentHTML` с
`accRowHtml`), касание меняет одну строку по `data-acc-id`. Проверяется
прогоном самих функций в node на поддельном DOM, а не грепом.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): вернуть в `loadMoreAccounts` безусловный
`renderAccounts(CUR_ACCS)` -> красный test_load_more_appends_only_the_new_page;
убрать ветку одной строки из `toggleAccSel` -> красный
test_one_tap_touches_one_row.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from miniapp_source import js_functions  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нет node")

_PRELUDE = r"""
let RENDERS = 0, APPENDED = 0, REPLACED = 0, SELBAR = 0;
const rows = [];
function mkRow(id){ const r = {id, set outerHTML(v){ REPLACED++; this._html=v; }}; return r; }
const listEl = {
  querySelector(sel){ return rows.length ? rows[0] : null; },
  insertAdjacentHTML(pos, html){ APPENDED += (html.match(/data-acc-id=/g)||[]).length; },
};
const document = {
  getElementById(id){ return id === 'accList' ? listEl : null; },
  querySelector(sel){
    const m = sel.match(/data-acc-id="(\d+)"/);
    return m ? rows.find(r => String(r.id) === m[1]) || null : null;
  },
};
let CUR_ACCS = [], ACC_HAS_MORE = true, ACC_FILTERED_TOTAL = null, ACC_OFFSET = 0;
let ACC_LOADING = false, ACC_SEL = new Set(), ACC_SEL_MODE = true;
function fleetIsWide(){ return false; }
function fleetRangePick(){ return false; }
function renderAccounts(list){ RENDERS++; }
function renderLoadMore(){}
function updateAccSelBar(){ SELBAR++; }
function _accFiltered(){ return CUR_ACCS; }
function _accQuery(o){ return '/q?offset=' + o; }
function toast(){}
function errRu(e){ return String(e); }
function accRowHtml(a){ return '<div class="acc-row" data-acc-id="' + a.id + '"></div>'; }
function page(from, n){ return Array.from({length:n}, (_, i) => ({id: from + i})); }
"""


def _run(body: str) -> dict:
    F = js_functions()
    src = "\n".join(F[n] for n in ("loadMoreAccounts", "toggleAccSel"))
    script = _PRELUDE + src + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(2); });"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_load_more_appends_only_the_new_page():
    res = _run(r"""
      CUR_ACCS = page(1, 100); ACC_OFFSET = 100; rows.push(mkRow(1));
      globalThis.api = async () => ({accounts: page(101, 100), page: {has_more: true}});
      api = globalThis.api;
      await loadMoreAccounts();
      console.log(JSON.stringify({RENDERS, APPENDED, n: CUR_ACCS.length}));
    """)
    assert res == {"RENDERS": 0, "APPENDED": 100, "n": 200}, res


def test_first_page_still_renders_whole_list():
    """Пустой список (первая загрузка) идёт обычным путём — там нечего дописывать."""
    res = _run(r"""
      api = async () => ({accounts: page(1, 100), page: {has_more: true}});
      await loadMoreAccounts();
      console.log(JSON.stringify({RENDERS, APPENDED}));
    """)
    assert res == {"RENDERS": 1, "APPENDED": 0}, res


def test_one_tap_touches_one_row():
    res = _run(r"""
      CUR_ACCS = page(1, 3000); for (const a of CUR_ACCS) rows.push(mkRow(a.id));
      toggleAccSel(1500, {});
      console.log(JSON.stringify({RENDERS, REPLACED, SELBAR, sel: ACC_SEL.has(1500)}));
    """)
    assert res == {"RENDERS": 0, "REPLACED": 1, "SELBAR": 1, "sel": True}, res
