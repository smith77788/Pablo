"""Пересечения аудитории: фильтр «только заметные», отчёт текстом, подписи.

У экосистемы из десятка каналов пар сорок пять, и почти все «почти не
совпадают» — нужные терялись в списке. Теперь пары сортируются по
пересечению, фильтр от 20% запоминается, а отчёт копируется текстом.
И баг: подпись кнопки резалась ПОСЛЕ экранирования, срез мог разрезать
«&amp;» пополам, и на кнопке появлялся мусор.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "eco_overlaps.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")

_ESC = r"""
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
"""


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _run(src):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_button_label_is_cut_before_escaping():
    src = _ESC + _fn("_eoShort") + r"""
console.log(JSON.stringify([_eoShort('Кофе & чай: лучшие рецепты'), _eoShort('Abcdefghijklmnop&x')]));
"""
    a, b = _run(src)
    assert a == "Кофе &amp; чай: лучшие…"
    # Раньше: esc(...).substring(0, 18) → «Abcdefghijklmnop&a» — обрубок сущности.
    assert b == "Abcdefghijklmnop&amp;x"


def test_filter_is_remembered_sorts_and_report_lists_visible_pairs():
    assert 'onchange="eoSetOnlyBig(this.checked)"' in JS
    assert 'onclick="eoCopyReport()"' in JS
    src = ("const _EO_BIG = 20;\nlet _eoPairs = [];\n" + _fn("_eoOnlyBig") + _fn("eoSetOnlyBig")
           + _fn("_eoVisiblePairs") + _fn("eoReportText") + r"""
let prefs = {};
function loadFormPrefs(k) { return prefs[k]; }
function saveFormPrefs(k, v) { prefs[k] = v; }
const document = {getElementById: () => null};
_eoPairs = [
  {name_a: 'A', name_b: 'B', overlap_pct: 5, shared: 3},
  {name_a: 'A', name_b: 'C', overlap_pct: 61.25, shared: 40},
  {name_a: 'B', name_b: 'C', overlap_pct: 20, shared: 9},
];
const all = eoReportText();
eoSetOnlyBig(true);
console.log(JSON.stringify({all, big: eoReportText(), prefs}));
""")
    out = _run(src)
    assert out["all"].split("\n") == [
        "Пересечения аудитории каналов:",
        "• A ↔ C: 61.3%, общих 40",
        "• B ↔ C: 20.0%, общих 9",
        "• A ↔ B: 5.0%, общих 3",
    ]
    assert out["big"].split("\n") == [
        "Пересечения аудитории каналов (от 20%):",
        "• A ↔ C: 61.3%, общих 40",
        "• B ↔ C: 20.0%, общих 9",
    ]
    assert out["prefs"] == {"eco_overlaps": {"only_big": True}}
