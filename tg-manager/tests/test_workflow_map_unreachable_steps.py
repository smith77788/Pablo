"""Схема сценария называет недостижимые шаги: к ним не ведёт ни один
переход от первого шага, значит до них не дойдёт ни один человек. Прогоны
всегда начинаются с шага 0 (workflow_runtime: INSERT ... current_step 0)."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/workflow_map.js").read_text(encoding="utf-8")
RT = (Path(__file__).resolve().parents[1] / "services/workflow_runtime.py").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _lost(steps):
    js = _fn("wfEdges") + _fn("wfUnreachable") + "console.log(JSON.stringify(wfUnreachable(" + json.dumps(steps) + ")))"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_step_after_stop_is_unreachable():
    steps = [{"type": "message", "text": "a"}, {"type": "action", "action": "stop"}, {"type": "message", "text": "b"}]
    assert _lost(steps) == [2]


def test_branch_target_is_reachable():
    steps = [{"type": "condition", "else_goto": 4}, {"type": "action", "action": "goto", "goto": 5},
             {"type": "message"}, {"type": "message"}, {"type": "message"}, {"type": "message"}]
    # 0 → 1 (да) и → 3 (нет, шаг 4); 1 → 4 (шаг 5); 2 никто не зовёт.
    assert _lost(steps) == [2]


def test_linear_and_empty():
    assert _lost([{"type": "message"}, {"type": "delay"}]) == []
    assert _lost([]) == []


def test_runs_start_at_first_step_and_map_warns():
    assert "VALUES ($1, $2, 'running', $3::jsonb, 0," in RT
    body = _fn("openWorkflowMap")
    assert "wfUnreachable(steps)" in body and "недостижим" in body
    assert "lost.has(i)" in _fn("_wfmSvg")
