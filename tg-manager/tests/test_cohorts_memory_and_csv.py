"""Когорты: шаг и показатель помнятся, таблица выгружается в CSV.

Шаг «по неделям» и показатель сбрасывались при каждом заходе, а цифры
таблицы можно было только переписать с экрана. Гоняем настоящие функции из
screens/cohorts.js в node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "cohorts.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _node(body):
    if not shutil.which("node"):
        pytest.skip("нет node")
    head = re.search(r"let _coGrain = [\s\S]*?const CO_METRICS = .*\n", JS).group(0)
    js = (head + "const prefs = {}; let loads = [];\n"
          "function loadFormPrefs(k) { return prefs[k] || {}; }\n"
          "function saveFormPrefs(k, o) { prefs[k] = Object.assign({}, prefs[k] || {}, o); }\n"
          "function _coScreen() { return {querySelector: () => true}; } function push() {}\n"
          "async function _coLoad() { loads.push(_coGrain + '/' + _coMetric); }\n"
          "function _coRender() {}\n"
          + _fn("coSetMetric") + _fn("openCohorts") + _fn("coCsv") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_grain_and_metric_survive_reopening():
    got = _node("(async () => { await openCohorts('week'); coSetMetric('buyers');"
                " _coGrain = 'month'; _coMetric = 'ltv'; await openCohorts();"
                " console.log(JSON.stringify(loads)); })();")
    assert got == ["week/ltv", "week/buyers"]


def test_csv_has_every_metric_per_cohort_in_plain_units():
    got = _node("const d = {periods: 2, money: {currency: 'RUB'}, cohorts: [\n"
                "  {label: 'Окт \"25', size: 10, ltv: [12.345, 20], active: [0.5, 0.1234], buyers: [0.2, null]}]};\n"
                "console.log(JSON.stringify(coCsv(d, 'month').split('\\n')));")
    assert got[0] == '"Пришли","Людей","Показатель","Месяц прихода","+1"'
    assert got[1] == '"Окт ""25","10","LTV, RUB","12.35","20"'
    assert got[2] == '"Окт ""25","10","Активны, %","50","12.3"'
    assert got[3] == '"Окт ""25","10","Покупали, %","20",""'


def test_screen_offers_export_and_entry_uses_memory():
    assert 'onclick="coExportCsv()"' in JS
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "open: () => openCohorts()}" in html
