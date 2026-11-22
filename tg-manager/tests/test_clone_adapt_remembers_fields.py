"""Клон и адаптация: набор полей для переноса берётся с прошлого запуска
(неизвестные ключи отбрасываются), повтор из истории — со своими полями."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/clone_adapt.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_default_fields_from_prefs():
    consts = re.search(r"\nconst CLA_FIELDS = \[[\s\S]*?\];\n", SRC).group(0)
    js = (consts + "let prefs={};const loadFormPrefs=k=>prefs[k]||{};" + _fn("claDefaultFields")
          + "const a=claDefaultFields();prefs.clone_adapt={fields:['desc','gone','commands']};"
            "const b=claDefaultFields();prefs.clone_adapt={fields:['gone']};"
            "console.log(JSON.stringify([a,b,claDefaultFields()]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    a, b, c = json.loads(out.stdout)
    assert a == c == ["name", "desc", "short", "commands"]
    assert b == ["desc", "commands"]


def test_wired_into_open_and_submit():
    assert "p.fields && p.fields.length ? p.fields : claDefaultFields()" in _fn("openCloneModal")
    sub = _fn("submitCloneAdapt")
    assert sub.index("saveFormPrefs('clone_adapt'") > sub.index("api('/api/miniapp/clone_adapt/submit'")
