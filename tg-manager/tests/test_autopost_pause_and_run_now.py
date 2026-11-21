"""Автопостинг: пауза, возобновление и «опубликовать сейчас» прямо в строке.

У строки автопостинга была одна кнопка — «Стоп». Пропустить праздники или
выложить пост вне очереди можно было только удалив его и настроив заново,
хотя ручки паузы, возобновления и запуска сейчас у операций давно есть.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "channel_manage.js").read_text(
    encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([^)]*\)[\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _const(name):
    m = re.search(r"\nconst " + name + r" = \{[\s\S]*?\n\};\n", JS)
    assert m, name
    return m.group(0)


_STUB = """
const box = {innerHTML: ''};
const document = {getElementById: () => box};
const esc = s => String(s); const num = n => String(n);
function _cmEvery(){ return 'каждый день'; } function _cmWhen(){ return 'завтра'; }
let calls = []; let asked = 0;
async function api(url, opt){ calls.push([url, opt && opt.method]); return ROWS; }
async function askConfirm(){ asked++; return true; }
function toast(){} function errRu(e){ return String(e); }
"""


def _node(js):
    if not shutil.which("node"):
        pytest.skip("нет node")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                     check=True).stdout)


def test_buttons_follow_the_status():
    rows = {"autoposts": [{"id": 1, "status": "pending", "text": "a"},
                          {"id": 2, "status": "paused", "text": "b"},
                          {"id": 3, "status": "running", "text": "c"}]}
    out = _node("const ROWS = " + json.dumps(rows) + ";" + _STUB + _fn("loadChAutoposts")
                + "\nloadChAutoposts(5, 'chdAutoposts').then(() => {"
                  "const h = box.innerHTML; const part = i => h.split('openOpDetail(' + i + ')')[1].split('</div>\\n    </div>')[0];"
                  "console.log(JSON.stringify([1,2,3].map(i => ["
                  " part(i).includes(\"'pause'\"), part(i).includes(\"'resume'\"),"
                  " part(i).includes(\"'run_now'\"), part(i).includes('stopChAutopost(' + i)])));});")
    assert out == [[True, False, True, True], [False, True, False, True], [False, False, False, True]]


def test_actions_hit_the_operation_routes_and_ask_before_publishing():
    out = _node("const ROWS = {autoposts: []};" + _STUB + _fn("loadChAutoposts") + _const("_CH_AP_ACT")
                + _fn("chAutopostAct")
                + "\n(async () => { await chAutopostAct(7, 'pause', 5, 'x');"
                  " await chAutopostAct(7, 'resume', 5, 'x'); await chAutopostAct(7, 'run_now', 5, 'x');"
                  " await chAutopostAct(7, 'drop', 5, 'x');"
                  " console.log(JSON.stringify({calls: calls.filter(c => c[1] === 'POST'), asked})); })();")
    assert [c[0] for c in out["calls"]] == ["/api/miniapp/operation/7/pause",
                                            "/api/miniapp/operation/7/resume",
                                            "/api/miniapp/operation/7/run_now"]
    # Пауза и публикация спрашивают, возобновление — нет; неизвестное действие не уходит.
    assert out["asked"] == 2
