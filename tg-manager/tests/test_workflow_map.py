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
const svg = globalThis._wfmSvg(steps, {steps: {"0": {reached: 12}, "1": {reached: 9, dropped: 4}}});
console.log(JSON.stringify({edges, rects: (svg.match(/<rect /g) || []).length,
  paths: (svg.match(/<path d="M/g) || []).length, has12: svg.includes('👥 12'),
  hasEnd: svg.includes('Конец'), drop4: svg.includes('↘ 4 ушли'),
  dropCount: (svg.match(/ушли</g) || []).length}));
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
    # «Отвалилось» — только там, где кто-то ушёл, а не «0 ушли» на каждом шаге.
    assert d["drop4"] and d["dropCount"] == 1


def test_wired_in_workflow_screen():
    html = miniapp_html()
    assert '<script src="screens/workflow_map.js"></script>' in html
    assert "openWorkflowMap(${Number(wfId)})" in html


def test_dropped_counts_runs_that_ended_badly_at_their_last_step():
    """На живом Postgres: прогон с ошибкой считается на последнем исполненном
    шаге, завершённый и идущий прогоны не считаются."""
    dsn = os.getenv("INFRAGRAM_TEST_DSN")
    if not dsn:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncio
    import asyncpg
    from services import workflow_engine as we
    owner = 88_500_001

    async def _go():
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            wf = await pool.fetchval(
                "INSERT INTO workflow_definitions(owner_id, name, steps) VALUES ($1,'drop test','[]') RETURNING id", owner)
            runs = {}
            for st in ("failed", "completed", "running", "expired"):
                runs[st] = await pool.fetchval(
                    "INSERT INTO workflow_runs(owner_id, workflow_id, status) VALUES ($1,$2,$3) RETURNING id",
                    owner, wf, st)
            log = [("failed", 0), ("failed", 1), ("completed", 0), ("completed", 1), ("completed", 2),
                   ("running", 0), ("expired", 0)]
            for st, idx in log:
                await pool.execute(
                    "INSERT INTO workflow_step_log(run_id, workflow_id, owner_id, step_idx, step_type, outcome) "
                    "VALUES ($1,$2,$3,$4,'message','sent')", runs[st], wf, owner, idx)
            d = await we.workflow_stats(pool, owner, wf)
            steps = {int(k): v for k, v in d["steps"].items()}
            assert steps[0].get("dropped") == 1      # истёкший ушёл после шага 1
            assert steps[1].get("dropped") == 1      # упавший — после шага 2
            assert "dropped" not in steps[2]         # завершённый не «ушёл»
        finally:
            await pool.execute("DELETE FROM workflow_step_log WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM workflow_runs WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM workflow_definitions WHERE owner_id=$1", owner)
            await pool.close()
    asyncio.run(_go())
