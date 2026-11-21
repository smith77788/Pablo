"""Мотор роста: период итогов виден и выбирается; сводка помнит график.

Итоги хаба всегда шли за 30 дней (`?days=30` вшито), а прирост каналов
подписывался «за период», не называя его. Сводка открывалась на
«Операции · 30 дней», что бы владелец ни выбрал в прошлый раз.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _body(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([^)]*\)[\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_growth_period_is_not_hardcoded():
    assert "growth/overview?days=30'" not in HTML
    load = _body("growthHubLoad")
    assert "'/api/miniapp/growth/overview?days=' + GROWTH_DAYS" in load
    assert "за ${GROWTH_DAYS} дн." in load
    assert 'id="growthHubDays"' in HTML


def test_growth_period_is_remembered():
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = ("let store = {growthhub:{days:90}}; let GROWTH_DAYS = 30; let loaded = 0;\n"
          "function push(){} function gpLoad(){} function growthHubLoad(){ loaded++; }\n"
          "function loadFormPrefs(k){ return store[k]||{}; }\n"
          "function saveFormPrefs(k,o){ store[k]=Object.assign({},store[k]||{},o); }\n"
          + _body("openGrowthHub") + "\n" + _body("growthHubSetDays") + "\n"
          "openGrowthHub(); const a = GROWTH_DAYS; growthHubSetDays(7);\n"
          "console.log(JSON.stringify([a, store.growthhub.days, loaded]));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    assert out == [90, 7, 2]


def test_dashboard_remembers_metric_and_period():
    assert "saveFormPrefs('dash', {metric: m})" in _body("dashSetMetric")
    assert "saveFormPrefs('dash', {days: n})" in _body("dashSetDays")
    op = _body("openDashboard")
    assert "loadFormPrefs('dash')" in op and "_dashMarkChips()" in op
