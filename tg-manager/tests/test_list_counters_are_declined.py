"""Счётчики над списками склоняются: «1 бот», «3 канала», «21 аккаунт».

Раньше подпись над списком ботов, каналов и аккаунтов всегда стояла во
множественном числе родительного падежа — «1 ботов», «3 каналов».
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mini_app"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
BOTS = (ROOT / "screens/bots_list.js").read_text(encoding="utf-8")
ACCS = (ROOT / "screens/accounts_list.js").read_text(encoding="utf-8")


def _fn(src, name):
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


def _run(js):
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_bot_counter_is_declined():
    js = _fn(INDEX, "plural") + _fn(BOTS, "_botsCountLabel") + (
        "console.log(JSON.stringify([0,1,3,5,11,21,22,112].map(_botsCountLabel)))")
    assert _run(js) == ["", "1 бот", "3 бота", "5 ботов", "11 ботов", "21 бот", "22 бота", "112 ботов"]


def test_channel_counter_is_declined():
    js = ("let CH_COUNTS = null, CH_TOTAL = 0;" + _fn(INDEX, "plural") + _fn(INDEX, "_chCountLabel")
          + "const out=[]; for (const n of [1,4,25]) { CH_COUNTS={all:n}; out.push(_chCountLabel()); }"
          + "CH_COUNTS=null; CH_TOTAL=2; out.push(_chCountLabel()); console.log(JSON.stringify(out))")
    assert _run(js) == ["1 канал", "4 канала", "25 каналов", "2 канала"]


def test_no_undeclined_counter_left():
    for src in (INDEX, BOTS, ACCS):
        assert not re.search(r"\+\s*' (ботов|каналов|аккаунтов)'\)?;", src)
