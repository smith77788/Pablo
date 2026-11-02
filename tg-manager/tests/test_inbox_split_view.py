"""Входящие на широком экране: список слева, переписка справа.

На компьютере строка разговора уводила на отдельный экран, и чтобы перейти к
следующему человеку, надо было вернуться назад. Теперь на широком экране
(body.wide) переписка открывается в правой колонке, а список остаётся на
месте. На телефоне — прежний переход на экран переписки.

Проверяется по исходнику и в Node: выбор места зависит от ширины в момент
клика, поля ответа и ИИ ищутся внутри того места, куда нарисована переписка
(в DOM могут жить оба), строки списка не сжимаются в ряд.
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
JS = os.path.join(ROOT, "mini_app", "screens", "crm_inbox.js")


def _src() -> str:
    return open(JS, encoding="utf-8").read()


def test_pane_and_screen_paths():
    src = _src()
    assert 'id="s-inbox-pane"' in src and "ib-split" in src
    # Решение по ширине — в момент клика, а не при загрузке.
    assert "function _ibSplit()" in src and "classList.contains('wide')" in src
    # Широкий экран сразу показывает первый разговор, а не пустую половину.
    assert "if (_ibSplit()) openInboxThread(0);" in src
    # Поля переписки ищутся внутри её места, не по всему документу.
    for fid in ("ibAiOut", "ibReplyText"):
        assert f"document.getElementById('{fid}')" not in src, fid
        assert f"_ibEl('{fid}')" in src, fid


def test_css_for_split_and_rows():
    html = miniapp_html()
    assert re.search(r"^\.ib-pane\{display:none\}", html, re.M), "на телефоне правой колонки нет"
    assert re.search(r"^body\.wide \.ib-pane\{display:block", html, re.M)
    assert re.search(r"^body\.wide \.ib-split\{display:flex", html, re.M)
    # .row объявлен ниже и с тем же весом — без .row.ib-row строка снова встанет в ряд.
    assert re.search(r"^\.row\.ib-row\{flex-direction:column", html, re.M)
    # Экраны из screens/*.js лежат прямо в body и без сдвига перекрывали меню слева.
    assert re.search(r"^body\.wide > \.screen\{left:var\(--nav-w\)\}", html, re.M)


_PROBE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function mk(id){ return {id, innerHTML:'', value:'', textContent:'', querySelector(sel){ return this.kids[sel.slice(1)] || null; }, kids:{}}; }
const pane = mk('s-inbox-pane-body'); const screen = mk('s-inboxthread-body');
pane.kids.ibReplyText = {value:'из колонки'}; screen.kids.ibReplyText = {value:'с экрана'};
const docEls = {ibReplyText: screen.kids.ibReplyText};
let wide = true;
global.document = { getElementById: id => docEls[id] || null,
  body: {classList:{contains: c => c === 'wide' && wide}} };
eval(src + '\n;globalThis._ibEl=_ibEl;globalThis._ibSplit=_ibSplit;globalThis.setHost=h=>{_ibHost=h};');
const out = {};
setHost(pane); out.inPane = _ibEl('ibReplyText').value;
setHost(screen); out.inScreen = _ibEl('ibReplyText').value;
setHost(null); out.noHost = _ibEl('ibReplyText').value;
out.splitWide = _ibSplit(); wide = false; out.splitNarrow = _ibSplit();
console.log(JSON.stringify(out));
"""


def test_fields_are_scoped_to_their_host(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    probe = tmp_path / "probe.js"
    probe.write_text(_PROBE, encoding="utf-8")
    r = subprocess.run([node, str(probe), JS], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d == {"inPane": "из колонки", "inScreen": "с экрана", "noHost": "с экрана",
                 "splitWide": True, "splitNarrow": False}
