"""Схема сценария: нажатие на шаг открывает его правку.

Схема была только для чтения: увидев на ней ошибку в ветке, владелец
закрывал схему и искал тот же шаг в столбце по номеру. Теперь шаг на схеме —
дверь в форму этого шага. Но только у своего, открытого сценария: у воронок
старого типа (legacy) шаги не правятся, и кнопки там быть не должно.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "workflow_map.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")

STEPS = [{"type": "message", "text": "Привет"}, {"type": "delay", "delay_minutes": 5}]


def _run(src):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def _prelude():
    return ("function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}\n"
            "function num(n){return String(n);}\n" + JS)


def test_steps_are_clickable_only_when_editable():
    src = _prelude() + f"""
const steps = {json.dumps(STEPS)};
console.log(JSON.stringify({{
  on: _wfmSvg(steps, null, true), off: _wfmSvg(steps, null, false)}}));
"""
    out = _run(src)
    assert out["on"].count('onclick="wfMapEditStep(') == 2
    assert 'onclick="wfMapEditStep(1)"' in out["on"]
    assert "wfMapEditStep" not in out["off"]


def test_map_of_the_open_workflow_is_editable_but_legacy_is_not():
    src = _prelude() + r"""
let calls = [], body = '';
const el = {classList: {add() {}}, setAttribute() {}};
const document = {getElementById: () => el, createElement: () => el, body: {appendChild() {}}};
function txt(id, h) { if (id === 'wfMapBody') body = h; }
function empty() { return ''; } function errHtml() { return ''; } function errRu() { return ''; }
let _currentWfId = 7;
function openWfAddStepModal(i) { calls.push(['edit', i]); }
function closeModal(id) { calls.push(['close', id]); }
let reply = {name: 'Тест', steps: """ + json.dumps(STEPS) + r"""};
async function api() { return reply; }
(async () => {
  await openWorkflowMap(7);
  const own = body.includes('wfMapEditStep(');
  reply = Object.assign({}, reply, {legacy_kind: 'funnel'});
  await openWorkflowMap(7);
  const legacy = body.includes('wfMapEditStep(');
  delete reply.legacy_kind;
  await openWorkflowMap(8);
  const other = body.includes('wfMapEditStep(');
  wfMapEditStep(1);
  console.log(JSON.stringify({own, legacy, other, calls}));
})();
"""
    out = _run(src)
    assert out == {"own": True, "legacy": False, "other": False,
                   "calls": [["close", "mo-wf-map"], ["edit", 1]]}
