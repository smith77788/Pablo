"""Командный центр, обзор проектов и SEO бота говорят по-человечески.

* «frozen» и любой незнакомый статус аккаунта всплывали в легенде кольца
  сырым английским словом; центр шкалы здоровья был подписан «trust».
* Пустой список проектов показывал голую кнопку без объяснения.
* «Применить рекомендацию» без изменений давало «⚠️ без изменений» — как сбой.
"""
import json
import re
import subprocess
from pathlib import Path

SCREENS = Path(__file__).resolve().parents[1] / "mini_app/screens"
CC = (SCREENS / "cmdcenter.js").read_text(encoding="utf-8")
PO = (SCREENS / "project_overview.js").read_text(encoding="utf-8")
BD = (SCREENS / "bot_detail.js").read_text(encoding="utf-8")


def _labels(statuses):
    start = CC.index("const CC_STATUS")
    end = CC.index("function _ccEnsureScreen")
    js = CC[start:end] + "console.log(JSON.stringify(%s.map(s => _ccStatus(s).label)))" % json.dumps(statuses)
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_status_shows_up_in_english():
    labels = _labels(["frozen", "deleted", "something_new", "active"])
    assert labels == ["Заморожены", "Удалены", "Другие", "Активные"]
    for lbl in labels:
        assert not re.search(r"[A-Za-z]", lbl)


def test_gauge_center_is_russian():
    assert "String(avg), 'доверие')" in CC
    assert "'trust')" not in CC


def test_empty_projects_explain_themselves():
    assert "Проектов пока нет." in PO


def test_seo_nothing_to_change_is_not_a_warning():
    assert "'⚠️ '+(errs||'без изменений')" not in BD
    assert "ℹ️ Менять нечего" in BD


def test_mesh_unknown_status_is_not_a_raw_code():
    mesh = (SCREENS / "bot_mesh.js").read_text(encoding="utf-8")
    for raw in ("t.status||''", "d.status||''", "h.outcome||''"):
        assert raw not in mesh, raw


def test_bot_commands_and_subscribers_are_spelled_out():
    assert "' подп.'" not in BD
    assert "plural(BS_TOTAL,'подписчик','подписчика','подписчиков')" in BD
    clear = BD[BD.index("async function clearBotCommands"):BD.index("async function openBotStats")]
    assert "if (!n) { toast('ℹ️ У бота нет команд'); return; }" in clear
    assert "Меню команд у бота в Telegram опустеет." in clear


def test_currency_list_names_every_main_currency():
    from services import owner_currency
    co = (SCREENS / "cohorts.js").read_text(encoding="utf-8")
    block = co[co.index("const MS_CUR_NAME"):co.index("function _msCurLabel")]
    for code in owner_currency.MAIN_CURRENCIES:
        assert code + ":" in block, code
    assert "esc(_msCurLabel(c))" in co


def test_project_journal_is_titled_by_name():
    index = (SCREENS.parent / "index.html").read_text(encoding="utf-8")
    m = re.search(r"\nfunction openOps\(projectId, projectName\) \{[\s\S]*?\n}\n", index)
    assert m and "'Журнал: ' + pname" in m.group(0)
    assert "openOps(project.id, project.name)" in PO


def test_flood_purge_names_how_many():
    fg = (SCREENS / "flood_guard.js").read_text(encoding="utf-8")
    index = (SCREENS.parent / "index.html").read_text(encoding="utf-8")
    plural = re.search(r"\nfunction plural\([\s\S]*?\n}\n", index).group(0)
    fn = re.search(r"\nfunction _fgPurgeText\([\s\S]*?\n}\n", fg).group(0)
    js = plural + fn + "console.log(JSON.stringify([_fgPurgeText(true, 3), _fgPurgeText(false, 1), _fgPurgeText(true, 0)]))"
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    hard3, soft1, hard0 = json.loads(out.stdout)
    assert hard3 == "Удалить 3 накрученных подписчика БЕЗВОЗВРАТНО?"
    assert soft1 == "Убрать 1 накрученного подписчика из аудитории (мягко, можно вернуть)?"
    assert hard0 == "Удалить накрученных подписчиков БЕЗВОЗВРАТНО?"
    assert "toast('ℹ️ Помеченных как накрутка нет')" in fg
