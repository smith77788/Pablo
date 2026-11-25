"""«Потянуть вниз — обновить»: жест, которого не было ни на одном экране.

Обновить список можно было двумя способами: уйти с экрана и зайти снова или
нажать вкладку второй раз (о втором нигде не написано). На телефоне
обновление списка — жест: владелец смотрит на очередь операций и тянет вниз,
потому что так работает всё остальное у него в телефоне, и до этого получал
нативный «отскок» и ничего больше.

Экран объявляет свою загрузку сам — `data-reload="loadOps"` в разметке.
Проверяется поведение на настоящих функциях (node), а не наличие строк: стенд
подменяет document/экраны/палец и смотрит, позовётся ли загрузка.
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
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
SCREENS = ROOT / "mini_app" / "screens"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")

# Стенд собирает функции по именам, поэтому список обязан идти за кодом:
# uxReloadFn теперь спрашивает карту «экран → чем открывается» (uxScreenFn), и
# без неё стенд падает ReferenceError на заведомо живом коде.
_NAMES = ("uxActiveScreen", "uxPullPhase", "uxScreenFn", "uxReloadFn", "uxPullBar",
          "uxPullStart", "uxPullMove", "uxPullEnd")


def _fn(name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", KIT)
    assert m, name
    return m.group(0)


_STAND = """
let UX_SCREEN_FN = {}, UX_PENDING_FN = '';
const made = {};
function screen(id, reload, top) {
  return made[id] = {id, scrollTop: top || 0, dataset: {reload: reload || ''},
    classList: {_on: new Set(['screen', 'show']),
      add(c) { this._on.add(c); }, remove(c) { this._on.delete(c); },
      contains(c) { return this._on.has(c); }}};
}
const bar = {id: 'uxPullBar', textContent: '', style: {display: 'none'}};
made['uxPullBar'] = bar;
const document = { getElementById: id => made[id] || null,
                   querySelectorAll: () => Object.values(made).filter(
                     x => x.classList && x.classList.contains('show')) };
const window = {};
let calls = [];
window.loadOps = function () { calls.push('ops'); };
const setTimeout = () => 0;
let UX_PULL = null;
const UX_PULL_AT = 70;
function touch(y) { return {touches: [{clientY: y}]}; }
"""


def _run(src: str):
    body = _STAND + "".join(_fn(n) for n in _NAMES) + src
    return json.loads(subprocess.check_output(["node", "-e", body], text=True))


def test_a_long_pull_runs_the_screens_own_load():
    out = _run(r"""
screen('s-ops', 'loadOps', 0);
uxPullStart(touch(100));
uxPullMove(touch(130));              // мало — только подсказка
const hint = bar.textContent;
uxPullMove(touch(200));              // хватит
const armed = bar.textContent;
uxPullEnd();
console.log(JSON.stringify({hint, armed, calls}));
""")
    assert "Потяните" in out["hint"]
    assert "Отпустите" in out["armed"]
    assert out["calls"] == ["ops"], "жест не позвал загрузку экрана"


def test_a_short_pull_changes_nothing():
    out = _run(r"""
screen('s-ops', 'loadOps', 0);
uxPullStart(touch(100));
uxPullMove(touch(108));
uxPullEnd();
console.log(JSON.stringify({calls, bar: bar.textContent, display: bar.style.display}));
""")
    assert out["calls"] == []
    assert out["display"] == "none"


def test_the_gesture_does_not_steal_scrolling():
    """Тянуть можно только с самого верха, иначе жест отбирает прокрутку."""
    out = _run(r"""
screen('s-ops', 'loadOps', 340);     // человек уже листает список
uxPullStart(touch(100));
uxPullMove(touch(220));
uxPullEnd();
const mid = calls.length;
// и наоборот: начали сверху, но экран успел прокрутиться — жест отменяется
const s = screen('s-ops2', 'loadOps', 0);
uxPullStart(touch(100));
s.scrollTop = 120;
uxPullMove(touch(220));
uxPullEnd();
console.log(JSON.stringify({mid, after: calls.length}));
""")
    assert out == {"mid": 0, "after": 0}


def test_a_screen_without_a_load_keeps_the_native_bounce():
    out = _run(r"""
screen('s-settings', '', 0);         // экран не объявил загрузку
uxPullStart(touch(100));
uxPullMove(touch(220));
uxPullEnd();
console.log(JSON.stringify({calls, display: bar.style.display}));
""")
    assert out["calls"] == [] and out["display"] == "none"


def test_a_stale_name_in_the_markup_is_not_a_load():
    out = _run(r"""
screen('s-ghost', 'loadThatWasRenamed', 0);
uxPullStart(touch(100));
uxPullMove(touch(220));
uxPullEnd();
console.log(JSON.stringify({calls, display: bar.style.display}));
""")
    assert out["calls"] == [], "имени больше нет в коде — «обновляем…» было бы ложью"


def test_every_declared_load_exists_and_needs_no_arguments():
    """`data-reload` зовётся без аргументов, значит обязательный — поломка.

    Имя в разметке видит и поиск по репозиторию, и сторож мёртвых функций;
    переименование функции без правки атрибута — тихая потеря жеста.
    """
    src = HTML + "".join(p.read_text(encoding="utf-8") for p in SCREENS.glob("*.js"))
    names = re.findall(r'data-reload="([A-Za-z0-9_]+)"', HTML)
    assert len(names) >= 20, f"жест подключён слишком узко: {names}"
    for name in sorted(set(names)):
        m = re.search(r"\bfunction " + name + r"\(([^)]*)\)", src)
        assert m, f"data-reload={name}: такой функции нет"
        args = [a.strip() for a in m.group(1).split(",") if a.strip()]
        # Параметр без значения по умолчанию допустим, только если это флаг
        # «дописать страницу»: без аргумента он приходит undefined, то есть
        # ложью, и загрузка идёт с первой страницы — ровно то, что нужно
        # обновлению. Любой другой параметр жест не заполнит ничем.
        for a in args:
            assert "=" in a or a.startswith("...") or a in ("append", "more"), (
                f"{name}({m.group(1)}): жест зовёт без аргументов, "
                f"обязательный параметр «{a}» означает поломку")


def test_the_gesture_is_actually_wired():
    init = KIT[KIT.index("function uxInit()"):]
    init = init[:init.index("\n}\n")]
    for ev, fn in (("touchstart", "uxPullStart"), ("touchmove", "uxPullMove"),
                   ("touchend", "uxPullEnd"), ("touchcancel", "uxPullEnd")):
        assert f"'{ev}', {fn}" in init, f"{ev} не подключён"
    assert "uxPullBar" in init, "полоску жеста никто не создаёт"
    assert ".ux-pullbar{" in HTML, "у полоски нет стиля — её не будет видно"
