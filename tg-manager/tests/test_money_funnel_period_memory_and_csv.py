"""Воронка денег: период запоминается, кампании выгружаются в CSV.

Раньше вход из меню всегда открывал «30 дней» — владелец, смотрящий квартал,
переключал период каждый раз. И таблицу кампаний нельзя было вынести в отчёт:
переписывали руками. Деньги в CSV, как и на экране, по валютам не складываются.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "money_funnel.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def _run(src):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_period_is_remembered_and_menu_entry_uses_it():
    assert "openMoneyFunnel()}" in HTML and "openMoneyFunnel(30)" not in HTML
    src = ("const MF_PERIODS = [[7, '7 дней'], [30, '30 дней'], [90, '90 дней']];\nlet _mfDays = 30;\n"
           + _fn("_mfPickDays") + r"""
let prefs = {};
function saveFormPrefs(k, v) { prefs[k] = v; }
function loadFormPrefs(k) { return prefs[k]; }
const first = _mfPickDays();
const picked = _mfPickDays(90);
const reopened = _mfPickDays();
const junk = _mfPickDays(13);
console.log(JSON.stringify({first, picked, reopened, junk, prefs}));
""")
    out = _run(src)
    assert out == {"first": 30, "picked": 90, "reopened": 90, "junk": 90,
                   "prefs": {"money_funnel": {"days": 90}}}


def test_csv_rows_keep_currencies_apart():
    assert 'onclick="mfExportCsv()"' in JS
    src = _fn("_mfCsvMoney") + _fn("mfCsv") + r"""
const rows = [
  {campaign_id: 5, name: 'Весна "А"', steps: {reached: 100, subscribed: 20, replied: 10, bought: 2},
   revenue: [{currency: 'RUB', total_cents: 150000}, {currency: 'USD', total_cents: 2500}],
   spend: {amount: 500, currency: 'RUB'}, cost_per_lead: 50.4, romi: 2},
  {campaign_id: 0, name: 'Без рассылки', steps: {subscribed: 3, replied: 1, bought: 0}, revenue: []},
];
console.log(JSON.stringify(mfCsv(rows, 90).split('\r\n')));
"""
    lines = _run(src)
    assert lines[0] == '"Воронка денег за 90 дней"'
    assert lines[1].startswith('"Кампания";"Доставлено"')
    assert lines[2] == '"Весна ""А""";"100";"20";"10";"2";"1500 RUB + 25 USD";"500 RUB";"50";"200"'
    assert lines[3] == '"Без рассылки";"";"3";"1";"0";"";"";"";""'
