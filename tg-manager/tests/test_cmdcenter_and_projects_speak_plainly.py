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
