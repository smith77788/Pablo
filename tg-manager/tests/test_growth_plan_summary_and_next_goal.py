"""План роста: сводка над списком целей и следующая цель тому же каналу.

Раньше с десятком целей нельзя было понять, сколько отстаёт, не открывая
каждую. А «Поставить следующую» после выполненной цели открывала форму с
первым каналом в списке — владелец ставил цель не тому каналу.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "growth_plan.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _run(src):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_summary_counts_on_track_behind_and_done():
    src = _fn("_gpSummary") + r"""
const goals = [
  {status: 'active', progress: {state: 'on_track'}},
  {status: 'active', progress: {state: 'ahead'}},
  {status: 'active', progress: {state: 'stalled'}},
  {status: 'active', progress: {state: 'overdue'}},
  {status: 'active', progress: {state: 'behind'}},
  {status: 'done', progress: {state: 'done'}},
];
// Кнопка «📋 Отчёт» живёт в той же строке сводки (test_growth_plan_copy_report),
// здесь сверяем только счётчики.
console.log(JSON.stringify(_gpSummary(goals)
  .replace(/<span class="sec-btn"[\s\S]*?<\/span>/g, '').replace(/<[^>]+>/g, '')));
"""
    assert _run(src) == "в работе: 5 · успеваете: 2 · отстают: 3 · выполнено: 1"


def test_next_goal_preselects_the_same_channel():
    src = "const _gpNum = n => String(n);\n" + _fn("gpOpenNew") + _fn("gpAct") + r"""
let backs = 0, toasts = [];
const sel = {options: [], value: '', innerHTML: '',
  add(o) { this.options.push(o); if (this.options.length === 1) this.value = o.value; }};
function Option(label, value) { return {label, value}; }
const els = {gpAsset: sel};
const document = {getElementById: id => els[id] || (els[id] = {style: {}, value: '', scrollIntoView() {}})};
function txt() {} function toast(m) { toasts.push(m); } function back() { backs++; }
function gpAssetChanged() {}
const GP = {data: {goals: [{status: 'done', asset_kind: 'channel', asset_id: 2}],
  assets: {channels: [{id: 1, name: 'A', current: 5}, {id: 2, name: 'B', current: 9}], bots: []}},
  goal: {asset_kind: 'channel', asset_id: 2}};
gpAct({kind: 'new_goal'});
console.log(JSON.stringify({value: sel.value, backs, toasts}));
"""
    out = _run(src)
    assert out == {"value": "channel:2", "backs": 1, "toasts": []}
