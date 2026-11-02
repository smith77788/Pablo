"""Ярлык мини-аппа на главном экране (Telegram Bot API 8.0).

Строка в настройках обязана говорить правду: ярлык уже есть, можно добавить,
или клиент Telegram этого не умеет. Проверяется по исходнику и в Node: три
состояния и отсутствие кнопки там, где нажать её бессмысленно.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.miniapp_source import miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "mini_app", "screens", "home_screen.js")


def test_wired_in_settings_and_loaded():
    html = miniapp_html()
    assert '<script src="screens/home_screen.js"></script>' in html
    assert 'id="set-home-btn"' in html and 'onclick="addAppToHomeScreen()"' in html
    assert "loadHomeScreenStatus()" in html
    src = open(JS, encoding="utf-8").read()
    assert "isVersionAtLeast('8.0')" in src and "checkHomeScreenStatus" in src
    assert "homeScreenAdded" in src


_PROBE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const els = {};
function el(id){ return els[id] || (els[id] = {id, textContent:'', style:{display:''}}); }
global.document = { getElementById: el };
global.toast = () => {};
const out = {};
function run(tgObj, status) {
  for (const k of Object.keys(els)) delete els[k];
  global.tg = tgObj;
  eval(src + '\n;globalThis._hsRender=_hsRender;globalThis.loadHomeScreenStatus=loadHomeScreenStatus;');
  globalThis.loadHomeScreenStatus();
  if (status) globalThis._hsRender(status);
  return {lbl: el('set-home-status').textContent, btn: el('set-home-btn').style.display};
}
const modern = {isVersionAtLeast: v => true, addToHomeScreen(){}, checkHomeScreenStatus(cb){ cb('missed'); }, onEvent(){}};
out.old = run({isVersionAtLeast: v => false, onEvent(){}});
out.missed = run(modern);
out.added = run(modern, 'added');
out.unsupported = run(modern, 'unsupported');
console.log(JSON.stringify(out));
"""


def test_three_honest_states_in_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    probe = tmp_path / "probe.js"
    probe.write_text(_PROBE, encoding="utf-8")
    r = subprocess.run([node, str(probe), JS], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert "8.0" in d["old"]["lbl"] and d["old"]["btn"] == "none"
    assert d["missed"]["btn"] == "" and "одним касанием" in d["missed"]["lbl"]
    assert d["added"]["btn"] == "none" and "Уже" in d["added"]["lbl"]
    assert d["unsupported"]["btn"] == "none"
