"""Закреплённое — на главном; разделы приложения — в общем поиске.

Каталог открывают не чтобы читать каталог, а чтобы попасть в свои пять-шесть
разделов: значит им место на первом экране, а не за двумя нажатиями.

И второе: раздел приложения искали ОТДЕЛЬНЫМ полем в боковом меню, то есть
надо было помнить, что «где включить прогрев» ищут не там, где «тот номер».
Теперь оба ответа даёт одно поле — разделы ищутся по самому каталогу, а не по
второму списку, который разъехался бы с ним при первом переименовании.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIT = (ROOT / "mini_app" / "screens" / "ux_kit.js").read_text(encoding="utf-8")
FA = (ROOT / "mini_app" / "screens" / "find_all.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(src: str, name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


def test_the_home_shelf_shows_pinned_and_disappears_when_empty():
    stand = """
let _LS = {};
const localStorage = { getItem: k => _LS[k] == null ? null : _LS[k],
                       setItem: (k, v) => { _LS[k] = String(v); } };
const UX_NAV_KEY = 'k';
const box = {id: 'homePinned', innerHTML: ''};
const document = { getElementById: id => id === 'homePinned' ? box : null };
"""
    src = stand + _fn(KIT, "uxNavMemory") + _fn(KIT, "uxNavEsc") + _fn(KIT, "uxPinnedHome") + r"""
uxPinnedHome();
const empty = box.innerHTML;
_LS[UX_NAV_KEY] = JSON.stringify({pinned: [
  {lbl: "Прогрев <b>", ico: '🌱', h: "openWarmup()"},
  {lbl: "Жалобы", ico: '📣', h: "openComplaints()"}], recent: []});
uxPinnedHome();
console.log(JSON.stringify({empty, full: box.innerHTML}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["empty"] == "", "пустая полка занимает место и ничего не говорит"
    assert "openWarmup()" in out["full"] and "openComplaints()" in out["full"]
    assert "&lt;b&gt;" in out["full"], "подпись из памяти — текст, а не разметка"
    assert "Закреплённое" in out["full"]


def test_sections_are_searched_in_the_catalog_itself():
    stand = """
const tiles = [
  {lbl: 'Прогрев аккаунтов', ico: '🌱', go: "openWarmup()"},
  {lbl: 'Жалобы', ico: '📣', go: "openComplaints()"},
];
const MODULE_DESCS = {'Жалобы': 'Отправка жалоб на чужие каналы и боты'};
function tileNode(t) {
  return {
    querySelector: sel => sel === '.mgmt-tile-lbl' ? {textContent: t.lbl}
                        : sel === '.mgmt-tile-ico' ? {dataset: {}, textContent: t.ico} : null,
    getAttribute: () => t.go,
  };
}
const more = { querySelectorAll: () => tiles.map(tileNode) };
const document = { getElementById: id => id === 's-more' ? more : null };
"""
    src = stand + _fn(FA, "faSections") + r"""
console.log(JSON.stringify({
  byName: faSections('прогрев').map(x => x.title),
  byDesc: faSections('жалоб').map(x => x.title),
  byMeaning: faSections('каналы').map(x => x.title),   // нашлось по описанию
  short: faSections('п').length,
  none: faSections('ничего такого').length,
}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["byName"] == ["Прогрев аккаунтов"]
    assert out["byDesc"] == ["Жалобы"]
    assert out["byMeaning"] == ["Жалобы"], "описание раздела — тоже то, что помнят"
    assert out["short"] == 0, "по одной букве нашлось бы всё подряд"
    assert out["none"] == 0


def test_both_are_wired():
    assert 'id="homePinned"' in HTML
    assert "uxPinnedHome();" in HTML, "главный экран не показывает закреплённое"
    assert "uxPinnedHome();" in KIT, "звезда в каталоге не обновляет полку на главном"
    assert "faSections(d.q)" in FA, "разделы не попадают в общий поиск"
    assert "Разделы приложения" in FA
