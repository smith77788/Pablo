"""Командный центр: автообновление раз в минуту, выбор запоминается.

Экран держат открытым, пока идёт кампания, и жали ⟳ каждые пару минут.
Галочка обновляет данные на месте. Таймер живёт, только пока экран сверху:
ушли с экрана — фоновые запросы прекращаются. Сбой фонового обновления не
затирает экран, а честно говорит, что данные прежние.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "cmdcenter.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def test_error_is_russian():
    assert "e.message" not in JS
    assert "_ccAutoRow() + _ccRender(d)" in _fn("openCmdCenter")


def test_ticks_only_while_on_top_and_keeps_data_on_failure():
    src = ("const _CC_EVERY_MS = 60000;\n" + "".join(_fn(n) for n in (
        "_ccAutoOn", "_ccOnTop", "_ccAutoRow", "ccSetAuto", "_ccSchedule", "_ccTick")) + r"""
let prefs = {}, timers = [], calls = 0, fail = false;
function loadFormPrefs(k) { return prefs[k]; }
function saveFormPrefs(k, v) { prefs[k] = v; }
function setTimeout(fn, ms) { timers.push([fn, ms]); return timers.length; }
function clearTimeout() {}
function _ccRender(d) { return '[данные ' + d.n + ']'; }
const els = {ccBody: {innerHTML: ''}, ccUpdated: {textContent: ''}};
const document = {visibilityState: 'visible', getElementById: id => els[id]};
let STACK = ['s-home', 's-cmdcenter'];
async function api() { calls++; if (fail) throw new Error('x'); return {n: calls}; }
(async () => {
  _ccSchedule();
  const noAuto = timers.length;
  ccSetAuto(true);
  const armed = timers.length;
  await timers.pop()[0]();
  const body1 = els.ccBody.innerHTML;
  fail = true;
  await timers.pop()[0]();
  const afterFail = {body: els.ccBody.innerHTML, note: els.ccUpdated.textContent};
  STACK.push('s-ops');
  fail = false;
  await timers.pop()[0]();
  const away = {calls, timers: timers.length};
  console.log(JSON.stringify({noAuto, armed, body1, afterFail, away, prefs}));
})();
""")
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["noAuto"] == 0
    assert out["armed"] == 1
    assert out["body1"].endswith("[данные 1]") and 'id="ccAuto" checked' in out["body1"]
    assert out["afterFail"] == {"body": out["body1"], "note": "не обновилось — данные прежние"}
    # Ушли с экрана: запроса нет, новый таймер не ставится.
    assert out["away"] == {"calls": 2, "timers": 0}
    assert out["prefs"] == {"cmdcenter": {"auto": True}}
