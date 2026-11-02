"""Широкий экран: меню разделов слева, работа справа.

На компьютере мини-апп растягивался на всю ширину, а меню пряталось за ☰.
Раскладка в две колонки включается по ширине окна (body.wide от 960px),
а не по платформе. Проверяется по исходнику и в Node: класс ставится и
снимается по ширине, меню строится заранее, подсветка следует за goTab,
строка «Во весь экран» видна только на компьютере с Telegram 8.0.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from tests.miniapp_source import miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "mini_app", "screens", "desktop.js")


def test_loaded_and_wired():
    html = miniapp_html()
    assert '<script src="screens/desktop.js"></script>' in html
    assert 'id="set-full-row"' in html and 'onclick="toggleAppFullscreen()"' in html
    assert "renderFullscreenRow()" in html


def test_wide_css_rules():
    html = miniapp_html()
    rules = dict(re.findall(r"^(body\.wide [^{]+)\{([^}]*)\}", html, re.M))
    # Меню всегда на экране, контент сдвинут на его ширину.
    assert "transform:none" in rules["body.wide #navDrawer"]
    assert "margin-left:var(--nav-w)" in rules["body.wide #app"]
    # showTabbar() ставит display инлайном, поэтому без !important таббар вернётся.
    assert "display:none !important" in rules["body.wide #tabbar"]
    # Колонка читаемой ширины, модалки по центру, а не листом снизу.
    assert "880px" in rules["body.wide .screen"]
    assert "align-items:center" in rules["body.wide .mbg"]
    # Отступ под таббар больше не нужен.
    assert re.search(r"^body\.wide\{[^}]*--tab-h:0px", html, re.M)


_PROBE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const els = {};
function el(id){ return els[id] || (els[id] = {id, textContent:'', style:{display:''},
  classList:{s:new Set(), add(c){this.s.add(c)}, remove(c){this.s.delete(c)}}}); }
const bodyCls = new Set();
global.document = { getElementById: el, body: { classList: {
  toggle(c, on){ on ? bodyCls.add(c) : bodyCls.delete(c); }, contains(c){ return bodyCls.has(c); } } } };
const calls = [];
global.buildDrawer = () => calls.push('build');
global.markDrawerActive = () => calls.push('mark');
global.closeDrawer = () => calls.push('close');
global.toast = () => {};
const listeners = {};
global.window = { innerWidth: 1280, addEventListener(e, f){ listeners[e] = f; } };
global.window.goTab = function(t){ calls.push('goTab:' + t); return t; };
global.tg = { platform: 'tdesktop', isFullscreen: false, isVersionAtLeast: () => true,
  requestFullscreen(){ calls.push('req'); }, exitFullscreen(){ calls.push('exit'); }, onEvent(){} };
eval(src + '\n;globalThis.renderFullscreenRow=renderFullscreenRow;globalThis.applyWideLayout=applyWideLayout;globalThis.toggleAppFullscreen=toggleAppFullscreen;');
const out = {};
out.wideAtStart = bodyCls.has('wide');
out.builtAtStart = calls.includes('build');
calls.length = 0;
out.goTabRet = window.goTab('bots');
out.goTabCalls = calls.slice();
calls.length = 0;
window.innerWidth = 700; listeners.resize();
out.narrowAfterResize = !bodyCls.has('wide');
out.closedOnShrink = calls.includes('close');
calls.length = 0;
window.goTab('home');
out.noMarkWhenNarrow = !calls.includes('mark');
renderFullscreenRow();
out.rowDesktop = el('set-full-row').style.display;
out.btnDesktop = el('set-full-btn').textContent;
toggleAppFullscreen();
out.requested = calls.includes('req');
tg.isFullscreen = true; renderFullscreenRow();
out.btnFull = el('set-full-btn').textContent;
tg.platform = 'android'; renderFullscreenRow();
out.rowPhone = el('set-full-row').style.display;
tg.platform = 'tdesktop'; tg.isVersionAtLeast = () => false; renderFullscreenRow();
out.rowOld = el('set-full-row').style.display;
console.log(JSON.stringify(out));
"""


def test_layout_and_fullscreen_in_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    probe = tmp_path / "probe.js"
    probe.write_text(_PROBE, encoding="utf-8")
    r = subprocess.run([node, str(probe), JS], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["wideAtStart"] and d["builtAtStart"]
    assert d["goTabRet"] == "bots" and d["goTabCalls"] == ["goTab:bots", "mark"]
    assert d["narrowAfterResize"] and d["closedOnShrink"] and d["noMarkWhenNarrow"]
    assert d["rowDesktop"] == "flex" and d["btnDesktop"] == "Развернуть"
    assert d["requested"] and d["btnFull"] == "Свернуть"
    assert d["rowPhone"] == "none" and d["rowOld"] == "none"
