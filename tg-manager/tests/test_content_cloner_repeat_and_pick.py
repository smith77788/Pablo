"""Клонер контента: повтор из истории, поиск и «все/снять» в выборе каналов.

Повторить прошлый запуск можно было, только заново набрав источник и
отметив те же каналы по одному. Проверяем настоящие функции в node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\([^)]*\)[\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


_DOM = """
const mk = (ref, val, name) => {
  const b = {className:'cloner-ch', checked:false, value:String(val), dataset:{ref}};
  const l = {style:{display:'flex'}, dataset:{name}, querySelector: () => b};
  b.label = l; return b;
};
const boxes = [mk('@news', 11, 'новости'), mk('22', 22, 'спорт'), mk('@cars', 33, 'авто')];
const els = {clonerSrc:{value:'', scrollIntoView(){}}, clonerCount:{value:'10'}};
const document = {
  getElementById: id => els[id],
  querySelectorAll: sel => sel === '.cloner-ch' ? boxes : boxes.map(b => b.label),
};
let toasted = ''; function toast(t){ toasted = t; }
function plural(n,a,b,c){ return c; }
"""


def _node(js):
    if not shutil.which("node"):
        pytest.skip("нет node")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                     check=True).stdout)


def test_repeat_fills_source_count_and_same_channels():
    hist = [{"id": 7, "params": json.dumps({"source": "@donor", "msg_count": 25,
                                            "target_refs": ["@news", 22, "@gone"]})}]
    out = _node(_DOM + "let CLONER_HIST = " + json.dumps(hist) + ";\n" + _fn("clonerRepeat")
                + "\nclonerRepeat(7);\nconsole.log(JSON.stringify({src: els.clonerSrc.value,"
                  " n: els.clonerCount.value, on: boxes.map(b => b.checked), t: toasted}));")
    assert out["src"] == "@donor" and out["n"] == 25
    assert out["on"] == [True, True, False]
    # Канал, которого больше нет, назван, а не потерян молча.
    assert "1" in out["t"] and "нет" in out["t"]


def test_mark_all_respects_search():
    out = _node(_DOM + _fn("clonerFilter") + _fn("clonerMarkAll")
                + "\nclonerFilter('сп'); clonerMarkAll(true);"
                  " const a = boxes.map(b => b.checked);"
                  " clonerFilter(''); clonerMarkAll(true); const b = boxes.map(x => x.checked);"
                  " clonerMarkAll(false); const c = boxes.map(x => x.checked);"
                  " console.log(JSON.stringify([a, b, c]));")
    assert out == [[False, True, False], [True, True, True], [False, False, False]]


def test_history_row_offers_repeat():
    assert "clonerRepeat(${Number(r.id)})" in HTML
    assert 'data-ref="${esc(ref)}"' in HTML
