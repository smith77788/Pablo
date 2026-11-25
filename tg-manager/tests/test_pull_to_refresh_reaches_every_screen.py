"""Тянуть-обновить работает и там, где атрибут никто не дописал.

РАЗРЫВ. Явный `data-reload` стоял у 27 экранов, а всего их около двухсот: на
остальных жест не делал ничего, и это читается как «жест сломан», а не «на этом
экране нечего обновлять». Дописывать атрибут руками каждому экрану — то самое
перечисление руками, которое в этом проекте уже дважды оставляло половину
продукта без двери (каталог «Все функции», реестр дверей идемпотентности).

Вторая дорога: каталог знает, КАКОЙ функцией открывается каждый экран, и ровно
она и есть обновление. Имя запоминается при открытии и привязывается к экрану в
единственной двери навигации (`push` → `uxNoteScreenFn`).

Граница намеренная: берём только имя функции БЕЗ аргументов. Строка обработчика
не исполняется как код, а экран с аргументом (карточка конкретного бота) без
явного `data-reload` жеста не получает — перерисовать карточку по чужому id
хуже, чем не сделать ничего.
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


def _fn(src: str, name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


def _stand(extra: str) -> dict:
    prog = (
        "let UX_SCREEN_FN = {}, UX_PENDING_FN = '';\n"
        "const window = globalThis;\n"
        + _fn(KIT, "uxReloadFn") + _fn(KIT, "uxScreenFn") + _fn(KIT, "uxNoteScreenFn")
        + "\n" + extra
    )
    return json.loads(subprocess.check_output(["node", "-e", prog], text=True))


def test_the_screen_remembers_what_opened_it_and_the_gesture_uses_it():
    out = _stand("""
let CALLED = 0;
globalThis.openAccounts = function () { CALLED += 1; };
UX_PENDING_FN = 'openAccounts()';
uxNoteScreenFn('s-accounts');
const fn = uxReloadFn({ id: 's-accounts', dataset: {} });
if (fn) fn();
console.log(JSON.stringify({ found: !!fn, called: CALLED,
                             pendingCleared: UX_PENDING_FN === '' }));
""")
    assert out["found"] and out["called"] == 1, (
        "жест на экране без атрибута обязан звать функцию, которой экран открыли")
    assert out["pendingCleared"], "имя должно привязаться один раз, а не ко всем экранам"


def test_an_explicit_attribute_still_wins():
    out = _stand("""
globalThis.loadHome = function () { return 'home'; };
globalThis.openAccounts = function () { return 'accounts'; };
UX_PENDING_FN = 'openAccounts()';
uxNoteScreenFn('s-home');
const fn = uxReloadFn({ id: 's-home', dataset: { reload: 'loadHome' } });
console.log(JSON.stringify({ which: fn ? fn() : null }));
""")
    assert out["which"] == "home", "явный data-reload обязан иметь приоритет"


def test_a_handler_string_is_never_executed_as_code():
    out = _stand("""
globalThis.BAD = 0;
globalThis.boom = function () { globalThis.BAD = 1; };
for (const call of ["boom(); boom()", "boom(1)", "x=1;boom()", "boom", "openX(12)"]) {
  UX_PENDING_FN = call;
  uxNoteScreenFn('s-x');
  const fn = uxReloadFn({ id: 's-x', dataset: {} });
  if (fn) fn();
}
console.log(JSON.stringify({ bad: globalThis.BAD }));
""")
    assert out["bad"] == 0, (
        "строка обработчика исполнилась как код — имя функции обязано быть "
        "именем, а не выражением")


def test_a_screen_opened_with_an_argument_gets_no_gesture():
    out = _stand("""
globalThis.openBot = function (id) { return id; };
UX_PENDING_FN = 'openBot(42)';
uxNoteScreenFn('s-botdetail');
console.log(JSON.stringify({ fn: !!uxReloadFn({ id: 's-botdetail', dataset: {} }) }));
""")
    assert out["fn"] is False, (
        "карточка по id — не список: обновлять её вслепую нельзя")


def test_the_single_navigation_door_binds_the_screen():
    """Привязка стоит в push — иначе карта зависела бы от памяти автора экрана."""
    push = re.search(r"\nfunction push\(sid\) \{[\s\S]{0,700}", HTML)
    assert push, "не нашли push"
    assert "uxNoteScreenFn(sid)" in push.group(0), (
        "push больше не привязывает экран к функции — жест снова останется "
        "только у тех экранов, кому дописали атрибут")


def test_remembering_a_screen_also_arms_the_gesture():
    body = _fn(KIT, "uxRememberScreen")
    assert "UX_PENDING_FN" in body, (
        "открытие экрана из каталога больше не сообщает, чем его обновлять")
