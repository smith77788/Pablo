"""Операции: «сколько ещё ждать» в списке и в карточке операции.

Раньше у идущей операции было только «12/340» и «идёт уже 20 мин» — ждать
пять минут или до вечера, владелец считал в уме. Оценка — по средней
скорости с начала прогона, обновляется живыми событиями вместе с числами и
не показывается, пока данных мало (меньше трёх целей или первых 30 секунд).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def _eta(cases):
    src = (_fn("_opDuration") + _fn("_opEta") + "\nconst T0 = Date.parse('2026-11-23T10:00:00Z');\n"
           "console.log(JSON.stringify(" + json.dumps(cases) +
           ".map(c => _opEta(c[0], c[1], '2026-11-23T10:00:00Z', T0 + c[2] * 1000))));")
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


@needs_node
def test_eta_is_rough_and_honest():
    out = _eta([
        [100, 340, 600],     # 100 за 10 мин → 240 ещё за 24 мин
        [10, 1000, 3600],    # 10 в час → 99 ч
        [2, 100, 600],       # мало целей — молчим
        [50, 100, 10],       # первые секунды — молчим
        [100, 100, 600],     # уже всё
        [99, 100, 600],      # одна цель — меньше минуты
        [60, 180, 7200],     # 2 ч на 60 → ещё ровно 4 ч, без «0 мин»
    ])
    assert out == ["≈ ещё 24 мин", "≈ ещё 4 д 3 ч", "", "", "", "≈ меньше минуты", "≈ ещё 4 ч"]


def test_eta_is_on_both_screens_and_live():
    render = HTML[HTML.index("function renderOps()"):HTML.index("function opSummaryHtml(")]
    assert "_opEtaSpan(o)" in render, "в строке списка операций нет оценки"
    detail = HTML[HTML.index("async function openOpDetail("):][:6000]
    assert "_opEtaSpan(o)" in detail, "в карточке операции нет оценки"
    live = _fn("liveOpRows")
    assert "data-op-eta" in live and "_opEta(" in live, "оценка не обновляется живыми событиями"
    span = _fn("_opEtaSpan")
    assert "o.status !== 'running'" in span and "data-op-start" in span
