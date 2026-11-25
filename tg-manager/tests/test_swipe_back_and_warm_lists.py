"""Свайп от края — назад, и списки, открывающиеся последними данными.

ДВА МЕСТА, ГДЕ ПРИЛОЖЕНИЕ ЖДАЛО ВПУСТУЮ.

Первое: кнопка «←» живёт в левом верхнем углу, а экраны вложены на три-четыре
уровня (список → карточка → раздел карточки). Системный свайп Telegram
закрывает ВСЁ приложение, а не шаг назад, поэтому привычного жеста не хватало
ровно там, где он нужен чаще всего.

Второе: список аккаунтов и ботов открывался спиннером. Ответ по сотне
аккаунтов со статистикой идёт секунды, а в метро и в лифте — дольше, и всё
это время экран был пуст: приложение выглядело сломанным там, где оно просто
ждало сеть. Теперь сразу показываются последние данные — и НАЗЫВАЮТ СЕБЯ
полоской «показаны последние данные»: молча показать вчерашний список хуже,
чем пустой экран, потому что по нему принимают решения.
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
ACC = (ROOT / "mini_app" / "screens" / "accounts_list.js").read_text(encoding="utf-8")
BOTS = (ROOT / "mini_app" / "screens" / "bots_list.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", KIT)
    assert m, name
    return m.group(0)


_STAND = """
const UX_EDGE = 24, UX_BACK_AT = 64, UX_BACK_SLOPE = 48;
let UX_SWIPE = null;
let STACK = ['s-accdetail'];
let backs = 0;
function back() { backs++; STACK.pop(); }
function start(x, y) { return {touches: [{clientX: x, clientY: y}]}; }
function end(x, y) { return {changedTouches: [{clientX: x, clientY: y}]}; }
"""


def _run(src: str):
    body = _STAND + _fn("uxSwipeVerdict") + _fn("uxSwipeStart") + _fn("uxSwipeEnd") + src
    return json.loads(subprocess.check_output(["node", "-e", body], text=True))


def test_a_swipe_from_the_edge_is_one_step_back():
    out = _run(r"""
uxSwipeStart(start(6, 300));
uxSwipeEnd(end(140, 310));
console.log(JSON.stringify({backs, left: STACK.length}));
""")
    assert out == {"backs": 1, "left": 0}


def test_a_swipe_that_did_not_start_at_the_edge_is_scrolling():
    """Полосы чипов прокручиваются вбок — жест не должен отбирать прокрутку."""
    out = _run(r"""
uxSwipeStart(start(180, 300));       // палец встал посреди экрана
uxSwipeEnd(end(320, 300));
const mid = backs;
uxSwipeStart(start(6, 300));         // от края, но почти вертикально
uxSwipeEnd(end(80, 400));
const slope = backs;
uxSwipeStart(start(6, 300));         // от края, но коротко
uxSwipeEnd(end(40, 300));
console.log(JSON.stringify({mid, slope, short: backs}));
""")
    assert out == {"mid": 0, "slope": 0, "short": 0}


def test_from_a_root_screen_the_gesture_does_nothing():
    """«Назад» с корневого экрана закрыл бы приложение — этого никто не просил."""
    out = _run(r"""
STACK = [];
uxSwipeStart(start(4, 300));
uxSwipeEnd(end(200, 300));
console.log(JSON.stringify({backs}));
""")
    assert out == {"backs": 0}


def test_the_stale_note_appears_above_the_list_and_goes_away():
    stand = """
const kids = [];
const parent = {
  insertBefore(node, before) { kids.push(node); node.parentNode = parent; },
  removeChild(node) { const i = kids.indexOf(node); if (i >= 0) kids.splice(i, 1); },
};
const made = {accList: {id: 'accList', parentNode: parent}};
const document = {
  getElementById: id => made[id] || kids.find(k => k.id === id) || null,
  createElement: () => ({id: '', className: '', textContent: '', parentNode: null,
                         setAttribute() {}}),
};
"""
    out = json.loads(subprocess.check_output(
        ["node", "-e", stand + _fn("uxStale") + r"""
uxStale('accList', true);
uxStale('accList', true);            // повтор не плодит вторую полоску
const on = kids.length, text = kids[0] ? kids[0].textContent : '';
uxStale('accList', false);
uxStale('nope', true);               // нет такого списка — молчим
console.log(JSON.stringify({on, text, off: kids.length}));
"""], text=True))
    assert out["on"] == 1 and out["off"] == 0
    assert "последние данные" in out["text"]


def test_lists_warm_up_from_the_cache_and_say_so():
    assert "getCached('infragram_cache_accounts')" in ACC
    assert "setCache('infragram_cache_accounts'" in ACC
    assert "getCached('infragram_cache_bots')" in BOTS
    assert "setCache('infragram_cache_bots'" in BOTS
    assert "uxStale('accList', true)" in ACC and "uxStale('botList', true)" in BOTS
    # Срез в кэш не кладём: показанный из кэша фильтр — это не то, что выбрано.
    assert "_accUnfiltered()" in ACC
    assert ACC.count("_accUnfiltered()") >= 3
    # Ошибка обновления не затирает уже показанные строки.
    assert "Не удалось обновить список" in ACC and "Не удалось обновить список" in BOTS
    assert ".ux-stale{" in HTML


def test_the_gesture_is_wired():
    init = KIT[KIT.index("function uxInit()"):]
    init = init[:init.index("\n}\n")]
    assert "'touchstart', uxSwipeStart" in init
    assert "'touchend', uxSwipeEnd" in init
