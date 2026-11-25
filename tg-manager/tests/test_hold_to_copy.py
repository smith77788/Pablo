"""Долгий тап — скопировать номер, Telegram ID, ник и почту.

Номер аккаунта, его Telegram ID, ник контакта — это то, что переносят руками
в другой чат, в таблицу, в поддержку. Скопировать их было нельзя НИГДЕ: в
мини-аппе выделение текста отключено (иначе любой скролл превращается в
выделение), а кнопки «копировать» стояли только у ссылок, — владелец
переписывал номер с экрана глазами.

Проверяется поведение на настоящих функциях (node): что удержание копирует,
что прокрутка его отменяет, что копирует ЕДИНСТВЕННАЯ дверь буфера
(`copyToClipboard` — она честно отвечает об отказе и умеет запасной путь без
navigator.clipboard), и что значение ищется на строке, а не на том пикселе,
куда попал палец.
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


_STAND = """
const UX_HOLD_MS = 550;
let UX_HOLD = null;
let timers = [];
const setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length - 1; };
const clearTimeout = i => { if (timers[i]) timers[i].live = false; };
function fire() { timers.forEach(t => { if (t.live) { t.live = false; t.fn(); } }); }
let copied = [];
function copyToClipboard(v, msg) { copied.push([v, msg]); }
// Строка с значением, внутри неё — подпись и значок: палец попадает в них.
const row = {tagName: 'DIV', dataset: {copy: '+79990000001', copyWhat: 'Телефон скопирован'}};
const label = {tagName: 'SPAN', dataset: {}, parentNode: row};
const plain = {tagName: 'DIV', dataset: {}, parentNode: {tagName: 'BODY', dataset: {}}};
row.parentNode = {tagName: 'BODY', dataset: {}};
function touch(y, target) { return {touches: [{clientY: y}], target}; }
"""


def _run(src: str):
    body = _STAND + _fn("uxCopyTarget") + _fn("uxHoldStart") + _fn("uxHoldMove") \
        + _fn("uxHoldCancel") + src
    return json.loads(subprocess.check_output(["node", "-e", body], text=True))


def test_holding_a_row_copies_its_value():
    out = _run(r"""
uxHoldStart(touch(300, label));     // палец попал в подпись внутри строки
fire();
console.log(JSON.stringify({copied}));
""")
    assert out["copied"] == [["+79990000001", "📋 Телефон скопирован"]]


def test_scrolling_is_not_holding():
    """Палец поехал — это листание списка, а не удержание."""
    out = _run(r"""
uxHoldStart(touch(300, row));
uxHoldMove(touch(330, row));        // уехал на 30 пикселей
fire();
const afterScroll = copied.length;
uxHoldStart(touch(300, row));
uxHoldCancel();                     // отпустил раньше срока
fire();
console.log(JSON.stringify({afterScroll, afterLift: copied.length}));
""")
    assert out == {"afterScroll": 0, "afterLift": 0}


def test_a_row_without_a_value_is_not_copyable():
    out = _run(r"""
uxHoldStart(touch(300, plain));
fire();
console.log(JSON.stringify({copied, armed: UX_HOLD === null}));
""")
    assert out["copied"] == [] and out["armed"] is True


def test_the_values_people_retype_by_hand_are_copyable():
    """Телефон, Telegram ID, ник и почта — то, что переносят в другие места."""
    assert HTML.count("data-copy=") >= 6, "копируемых значений подозрительно мало"
    for what in ("Телефон скопирован", "Telegram ID скопирован",
                 "Ник скопирован", "Почта скопирована"):
        assert what in HTML, what
    # Копирует единственная дверь буфера, а не свой navigator.clipboard.
    assert "copyToClipboard(node.dataset.copy" in KIT
    assert "navigator.clipboard.writeText" not in KIT, "второй путь в буфер — второй обман об успехе"
    # Подсказка «удерживайте» обязана быть: невидимый жест не жест.
    assert HTML.count("Удерживайте, чтобы скопировать") >= 6
    init = KIT[KIT.index("function uxInit()"):]
    init = init[:init.index("\n}\n")]
    assert "'touchstart', uxHoldStart" in init and "'touchmove', uxHoldMove" in init
