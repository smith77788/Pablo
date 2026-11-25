"""«Итоги»: период 7 или 30 дней, выбор запоминается.

Раньше итоги были только за неделю: месяц к прошлому месяцу владелец считал
руками. Сервер период уже принимал (`?days=`), экран его не давал выбрать.
У каждого периода свой кэш — иначе после переключения на месяц показались
бы недельные числа под заголовком «за 30 дней».
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "summary.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_switch_to_month_is_remembered_and_fetches_its_own_period():
    src = ("const _SUM_PERIODS = [7, 30];\n" + _fn("_sumDays") + _fn("sumSetDays")
           + _fn("loadSummary") + r"""
let prefs = {}, urls = [], cacheSet = [], cacheGet = [], rendered = [];
function loadFormPrefs(k) { return prefs[k]; }
function saveFormPrefs(k, v) { prefs[k] = v; }
function getCached(k) { cacheGet.push(k); return k === 'infragram_cache_summary' ? {days: 7, week: true} : null; }
function setCache(k, v) { cacheSet.push(k); }
function renderSummary(d) { rendered.push(d.days); }
function homeLoadState() {}
const UI = {block: (t) => t, skeleton: () => ''};
const wrap = {innerHTML: '', appendChild() {}};
const document = {getElementById: id => id === 'summaryWrap' ? wrap : {setAttribute() {}},
                  createElement: () => ({setAttribute() {}})};
async function api(u) { urls.push(u); return {days: Number(u.split('=')[1])}; }
(async () => {
  await loadSummary();
  sumSetDays(30);
  await new Promise(r => setTimeout(r, 0));
  console.log(JSON.stringify({prefs, urls, cacheSet, cacheGet, rendered, title: wrap.innerHTML}));
})();
""")
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["urls"] == ["/api/miniapp/summary?days=7", "/api/miniapp/summary?days=30"]
    assert out["prefs"] == {"summary": {"days": 30}}
    assert out["cacheSet"] == ["infragram_cache_summary", "infragram_cache_summary_30"]
    # Месячный экран не берёт недельный кэш.
    assert out["cacheGet"][-1] == "infragram_cache_summary_30"
    assert out["rendered"] == [7, 7, 30]
    assert out["title"] == "📊 Итоги за 30 дней"


def test_period_row_is_rendered_and_errors_are_russian():
    assert "_sumPeriodRow(days)" in _fn("renderSummary")
    assert "e.message" not in JS


def test_period_switch_sits_inside_the_summary_block():
    """Переключатель периода стоял отдельной группой ПОСЛЕ блока итогов и
    выпадал из отступов экрана: «Период» и кнопки прилипали к левому краю.
    Теперь это UI.segmented внутри UI.block, под заголовком."""
    body = _fn("renderSummary")
    call = body[body.index("UI.block('📊 Итоги за '"):]
    call = call[:call.index(");") + 2]
    assert "_sumPeriodRow(days)" in call, "период снова вынесен за блок итогов"
    assert "UI.segmented(" in _fn("_sumPeriodRow")
    kit = (ROOT / "mini_app" / "screens" / "ui_kit.js").read_text(encoding="utf-8")
    css = (ROOT / "mini_app" / "ui_kit.css").read_text(encoding="utf-8")
    # Проверяем, что компонент ЕСТЬ и ЭКСПОРТИРОВАН, а не что он последний в
    # списке экспорта: «segmented }» краснело от любого нового компонента,
    # дописанного после него.
    assert "function segmented(" in kit
    exported = re.search(r"return \{([^}]*)\};", kit, re.S)
    assert exported and "segmented" in exported.group(1), (
        "компонент есть, но наружу не отдан — экран его не увидит")
    assert ".ui-seg-btn{" in css and "aria-pressed" in kit
