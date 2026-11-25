"""Общая механика экранов: возврат на место, «наверх», «нет сети».

Три вещи, которых не было ни на одном экране, хотя касаются они всех:

* `push()` ставил новому экрану scrollTop = 0, а `back()` не возвращал ничего —
  список на триста строк листали заново после каждого открытия карточки;
* вернуться к началу длинного списка можно было только пальцем;
* обрыв связи выглядел как сломанное приложение: экраны показывали ошибку
  загрузки, а причина не называлась нигде.

Проверяется поведение на настоящих функциях (node), а не наличие строк:
стенд подменяет document/экраны и смотрит, куда поедет прокрутка.
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

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", KIT)
    assert m, name
    return m.group(0)


def _run(src: str):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


_SCREENS = """
const made = {};
function screen(id, top) {
  return made[id] = {id, scrollTop: top || 0, classList: {
    _on: new Set(['screen']),
    add(c) { this._on.add(c); }, remove(c) { this._on.delete(c); },
    contains(c) { return this._on.has(c); } }};
}
const document = { getElementById: id => made[id] || null };
const requestAnimationFrame = fn => fn();
"""


def test_back_returns_to_the_same_place():
    src = _SCREENS + "const UX_SCROLL = Object.create(null);\n" \
        + _fn("uxRememberScroll") + _fn("uxRestoreScroll") + _fn("uxForgetScroll") + r"""
const list = screen('s-accounts', 1840);
const card = screen('s-accdetail', 0);
// уходим с длинного списка в карточку
uxRememberScroll('s-accounts');
uxForgetScroll('s-accdetail');
card.scrollTop = 0;
// возвращаемся
uxRestoreScroll('s-accounts');
const after = list.scrollTop;
// новый заход на тот же экран «сверху» (push) забывает позицию
uxForgetScroll('s-accounts');
uxRestoreScroll('s-accounts');
console.log(JSON.stringify({after, afterPush: list.scrollTop, card: card.scrollTop}));
"""
    assert _run(src) == {"after": 1840, "afterPush": 0, "card": 0}


def test_a_screen_never_seen_scrolled_opens_at_the_top():
    src = _SCREENS + "const UX_SCROLL = Object.create(null);\n" \
        + _fn("uxRememberScroll") + _fn("uxRestoreScroll") + r"""
const s = screen('s-ops', 500);
uxRememberScroll('s-ops');      // запомнили 500
screen('s-fresh', 0);
uxRememberScroll('s-fresh');    // ноль не запоминаем — это не позиция
uxRestoreScroll('s-fresh');
console.log(JSON.stringify({fresh: made['s-fresh'].scrollTop, ops: 'ok'}));
"""
    assert _run(src)["fresh"] == 0


def test_top_button_appears_only_on_a_long_scroll():
    src = _SCREENS + "const UX_TOP_AT = 600;\n" \
        + _fn("uxActiveScreen") + _fn("uxSyncTopButton") + r"""
const btn = {style: {display: 'none'}};
made['uxTopBtn'] = btn;
const s = screen('s-contacts', 100);
s.classList.add('show');
uxSyncTopButton(s);
const atTop = btn.style.display;
s.scrollTop = 1500;
uxSyncTopButton(s);
console.log(JSON.stringify({atTop, deep: btn.style.display}));
"""
    assert _run(src) == {"atTop": "none", "deep": ""}


def test_offline_bar_shows_and_reload_runs_only_on_a_tab():
    src = "let UX_OFFLINE = false;\n" + _SCREENS + _fn("uxOffline") + r"""
const bar = {style: {display: 'none'}};
made['uxNetBar'] = bar;
let reloaded = 0;
function uxReloadCurrent() { reloaded++; }
uxOffline(true);
const shown = bar.style.display;
uxOffline(true);                       // повтор не мигает и не обновляет
uxOffline(false);                      // связь вернулась — обновляем экран
console.log(JSON.stringify({shown, hidden: bar.style.display, reloaded}));
"""
    assert _run(src) == {"shown": "", "hidden": "none", "reloaded": 1}


def test_reload_leaves_inner_screens_alone():
    src = _fn("uxReloadCurrent") + r"""
let calls = [];
function loadHome() { calls.push('home'); }
const CUR = 'home';
let STACK = ['s-accdetail'];
uxReloadCurrent();                      // открыт вложенный экран — не трогаем
const inner = calls.length;
STACK = [];
uxReloadCurrent();
console.log(JSON.stringify({inner, outer: calls}));
"""
    assert _run(src) == {"inner": 0, "outer": ["home"]}


def test_navigation_is_wired_to_the_kit():
    """Механика бесполезна, если push/back её не зовут."""
    push = HTML[HTML.index("function push(sid"):]
    push = push[:push.index("\nfunction back()")]
    assert "uxRememberScroll(s.id)" in push, "push не запоминает, где стоял человек"
    assert "uxForgetScroll(sid)" in push, "переход вперёд обязан открывать экран сверху"
    back = HTML[HTML.index("\nfunction back()"):]
    back = back[:back.index("\n// ── Модули из нескольких экранов")]
    assert "uxRestoreScroll(prev)" in back, "back не возвращает на место"
    assert 'src="screens/ux_kit.js"' in HTML
