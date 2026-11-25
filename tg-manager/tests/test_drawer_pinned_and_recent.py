"""Каталог помнит своё: «Закреплённые» и «Недавние» над общим списком.

Каталог — 80+ разделов в 11 категориях, шесть экранов прокрутки на телефоне.
Человек ходит изо дня в день в пять-шесть, и каждый раз искал их заново: ни
закрепить своё, ни увидеть вчерашнее было нельзя (поиск по каталогу помогает,
только если помнишь название).

Проверяется поведение на настоящих функциях, а не наличие строк: стенд
подменяет localStorage и смотрит, что окажется в памяти и в разметке.
Отдельно — экранирование значения внутри onclick: именно на нём однажды
«не открывалось НИЧЕГО» (CLAUDE.md про данные в атрибуте).
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

# Стенд обязан знать ВСЁ, что зовёт проверяемая функция: uxNavPin обновляет и
# полку закреплённого на главном экране, и без неё стенд падал бы ReferenceError
# — то есть сообщал бы о поломке там, где её нет.
_NAMES = ("uxNavMemory", "uxNavSave", "uxNavRemember", "uxNavPinned", "uxNavPin",
          "uxNavBlocks", "uxNavEsc", "uxNavQ", "uxPinnedHome")


def _fn(name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", KIT)
    assert m, name
    return m.group(0)


def _kit() -> str:
    consts = "const UX_NAV_KEY='k';const UX_RECENT_MAX=6;const UX_PINNED_MAX=12;\n"
    store = """
let _LS = {};
const localStorage = { getItem: k => _LS[k] == null ? null : _LS[k],
                       setItem: (k, v) => { _LS[k] = String(v); } };
const document = { getElementById: () => null };
const toasts = [];
function toast(m) { toasts.push(m); }
"""
    return consts + store + "".join(_fn(n) for n in _NAMES)


def _run(src: str):
    return json.loads(subprocess.check_output(["node", "-e", _kit() + src], text=True))


def test_recent_keeps_the_last_six_without_repeats():
    out = _run(r"""
for (const n of ['Прогрев','Операции','Контакты','Прокси','Жалобы','Нотариус','Рой'])
  uxNavRemember(n, '•', 'open' + n.length + '()');
uxNavRemember('Контакты', '👥', 'openContacts()');   // был в середине — встаёт первым
const mem = uxNavMemory();
console.log(JSON.stringify({order: mem.recent.map(x => x.lbl), n: mem.recent.length}));
""")
    assert out["n"] == 6
    assert out["order"][0] == "Контакты"
    assert out["order"].count("Контакты") == 1
    assert "Прогрев" not in out["order"], "самое старое должно выпасть"


def test_pin_toggles_and_has_a_ceiling():
    out = _run(r"""
uxNavPin('Прогрев', '🌱', 'openWarmup()');
const on = uxNavPinned('Прогрев');
uxNavPin('Прогрев', '🌱', 'openWarmup()');          // второй раз — открепить
const off = uxNavPinned('Прогрев');
for (let i = 0; i < 14; i++) uxNavPin('Раздел' + i, '•', 'open' + i + '()');
console.log(JSON.stringify({on, off, n: uxNavMemory().pinned.length,
                            last: toasts[toasts.length - 1]}));
""")
    assert out["on"] is True and out["off"] is False
    assert out["n"] == 12, "потолок закреплённого не держится"
    assert "12" in out["last"] and "открепите" in out["last"].lower()


def test_blocks_render_pinned_first_and_do_not_repeat_in_recent():
    out = _run(r"""
uxNavRemember('Операции', '⚙️', 'openOps()');
uxNavRemember('Прогрев', '🌱', 'openWarmup()');
uxNavPin('Прогрев', '🌱', 'openWarmup()');
const html = uxNavBlocks();
console.log(JSON.stringify({
  html,
  pinnedFirst: html.indexOf('Закреплённые') < html.indexOf('Недавние'),
  warmInRecent: html.slice(html.indexOf('Недавние')).includes('Прогрев'),
}));
""")
    assert out["pinnedFirst"] is True
    assert out["warmInRecent"] is False, "закреплённое не должно дублироваться в недавнем"
    assert "openOps()" in out["html"] and "★" in out["html"] and "☆" in out["html"]


def test_quoting_survives_an_apostrophe_and_a_quote():
    """Атрибут onclick рвут апостроф и кавычка, а не угловые скобки."""
    out = _run(r"""
uxNavPin("O'Брайен & <b>", '•', "openX('a\"b')");
const html = uxNavBlocks();
console.log(JSON.stringify({html}));
""")
    html = out["html"]
    # Ни одной «голой» кавычки внутри значения атрибута, иначе он порвётся.
    for attr in re.findall(r'onclick="([^"]*)"', html):
        assert '"' not in attr
    assert "\\'" in html, "апостроф обязан быть экранирован для строки JS"
    assert "&amp;" in html and "&lt;b&gt;" in html


def test_drawer_uses_the_blocks_and_remembers_the_click():
    build = HTML[HTML.index("function buildDrawer()"):]
    build = build[:build.index("\nfunction navNorm")]
    assert "uxNavBlocks()" in build, "каталог не показывает закреплённое и недавнее"
    assert "uxNavRemember(" in build, "переход по каталогу не запоминается"
    assert "uxNavPin(" in build and "ni-pin" in build, "звезды закрепления нет на пунктах"
    # Ярлыки не должны «съедать» пункт каталога: seen пополняют только плитки.
    assert "seen.add(key)" in build
    assert ".ni-pin{" in HTML, "у звезды нет стиля — она сольётся с текстом"
