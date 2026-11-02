"""Схема сценария: шаги и переходы одной картинкой (screens/workflow_map.js).

Схема обязана рисовать те же переходы, что делает исполнитель
(services/workflow_runtime.py), иначе она хуже текста: человек поверит
стрелке, которой нет. Поэтому правила переходов проверяются в Node на
сценарии со всеми видами шагов, а картинка — на том, что каждому шагу есть
прямоугольник, а прыжку — дуга с подписью.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.miniapp_source import miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "mini_app", "screens", "workflow_map.js")

STEPS = [
    {"type": "message", "text": "Привет", "buttons": [
        {"text": "Хочу купить", "goto": 3}, {"text": "Сайт", "url": "https://x"}, {"text": "Позже"}]},
    {"type": "delay", "delay_minutes": 60},
    {"type": "condition", "condition": "has_tag", "condition_value": "vip", "else_goto": 6},
    {"type": "ask", "text": "Телефон?", "save_as": "phone", "timeout_goto": 99},
    {"type": "invoice", "amount": 100, "currency": "XTR"},
    {"type": "split", "branches": [{"label": "A", "weight": 50, "goto": 1}, {"label": "B", "weight": 50}]},
    {"type": "action", "action": "goto", "goto": 2},
    {"type": "action", "action": "stop"},
    {"type": "webhook", "url": "https://hook"},
]

_PROBE = r"""
const fs = require('fs');
global.esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
global.num = n => String(n);
eval(fs.readFileSync(process.argv[2], 'utf8') + '\n;globalThis.wfEdges=wfEdges;globalThis._wfmSvg=_wfmSvg;');
const steps = JSON.parse(process.argv[3]);
const edges = globalThis.wfEdges(steps);
const svg = globalThis._wfmSvg(steps, {steps: {"0": {reached: 12}}});
console.log(JSON.stringify({edges, rects: (svg.match(/<rect /g) || []).length,
  paths: (svg.match(/<path d="M/g) || []).length, has12: svg.includes('👥 12'),
  hasEnd: svg.includes('Конец')}));
"""


def _run():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(_PROBE)
        probe = f.name
    try:
        r = subprocess.run([node, probe, JS, json.dumps(STEPS)], capture_output=True,
                           text=True, timeout=30)
    finally:
        os.unlink(probe)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_edges_follow_the_runtime_rules():
    d = _run()
    e = {(x["from"], x["to"], x["label"]) for x in d["edges"]}
    # Кнопки-ветки: goto или «дальше»; кнопка-ссылка перехода не даёт; без
    # ответа — конец (timeout_goto не задан).
    assert (0, 2, "«Хочу купить»") in e and (0, 1, "«Позже»") in e
    assert not any(x["from"] == 0 and "Сайт" in x["label"] for x in d["edges"])
    assert (0, "end", "нет ответа") in e
    assert (1, 2, "") in e                                   # пауза → следующий
    assert (2, 3, "да") in e and (2, 5, "нет") in e          # условие с «иначе»
    assert (3, 4, "ответил") in e and (3, "end", "нет ответа") in e   # 99 — за концом
    assert (4, 5, "оплатил") in e and (4, "end", "не оплатил") in e
    assert (5, 0, "A · 50%") in e and (5, 6, "B · 50%") in e
    assert (6, 1, "переход") in e
    assert (7, "end", "стоп") in e
    assert (8, "end", "") in e                               # последний шаг → конец


def test_svg_draws_every_step_jumps_and_stats():
    d = _run()
    assert d["rects"] == len(STEPS) + 1          # шаги + «Конец»
    assert d["paths"] >= 8                       # прыжки и ветки — дугами
    assert d["has12"] and d["hasEnd"]


def test_wired_in_workflow_screen():
    html = miniapp_html()
    assert '<script src="screens/workflow_map.js"></script>' in html
    assert "openWorkflowMap(${wfId}" in html
