"""Фабрика ботов помнит режим, число ботов на аккаунт и способ имён, а числа
в её подписях склоняются («1 бот», «3 бота», а не «бот(ов)»)."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mini_app"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
BF = (ROOT / "screens/bot_factory.js").read_text(encoding="utf-8")


def _fn(src, name):
    one = re.search(r"\nfunction " + name + r"\(.*\}\n", src)
    if one and one.group(0).count("{") == one.group(0).count("}"):
        return one.group(0)
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


STUB = """
let _BF_MODE = 'new';
const PREFS = {};
function loadFormPrefs(k) { return PREFS[k] || {}; }
function saveFormPrefs(k, o) { PREFS[k] = Object.assign({}, PREFS[k] || {}, o); }
const EL = { bfCount: { value: '1' }, bfNameMode: { value: 'keywords' } };
const document = { getElementById: id => EL[id] || null };
"""


def _run(body):
    js = STUB + _fn(INDEX, "plural") + _fn(BF, "_bfRestorePrefs") + _fn(BF, "_bfSavePrefs") + _fn(BF, "_bfBotsWord") + body
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_saved_choice_comes_back():
    r = _run("""
      _BF_MODE = 'token'; _bfSavePrefs({ count: 4, name_mode: 'num' });
      _BF_MODE = 'new'; _bfRestorePrefs();
      console.log(JSON.stringify([_BF_MODE, EL.bfCount.value, EL.bfNameMode.value]));
    """)
    assert r == ["token", "4", "num"]


def test_garbage_in_prefs_is_ignored():
    r = _run("""
      PREFS.bot_factory = { mode: 'evil', count: 99, name_mode: 'x' }; _bfRestorePrefs();
      console.log(JSON.stringify([_BF_MODE, EL.bfCount.value, EL.bfNameMode.value]));
    """)
    assert r == ["new", "1", "keywords"]


def test_bots_word_is_declined():
    assert _run("console.log(JSON.stringify([1,2,5,21].map(_bfBotsWord)))") == ["бот", "бота", "ботов", "бот"]


def test_wiring():
    assert "if (!refresh) _bfRestorePrefs();" in BF
    assert "_bfSavePrefs({ count: cnt, name_mode: bfNameMode });" in BF
    assert "if (typeof _bfSavePrefs === 'function') _bfSavePrefs();" in _fn(INDEX, "bfSetMode")


def test_no_bracket_plurals_left():
    for src in (INDEX, BF, (ROOT / "screens/accounts_list.js").read_text(encoding="utf-8")):
        assert "(ов)" not in src
