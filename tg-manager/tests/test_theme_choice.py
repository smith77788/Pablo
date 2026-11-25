"""Тема выбирается вручную: авто, светлая, тёмная.

Мини-апп брал тему у Telegram и только у него. Тема в Telegram одна на всё
приложение, а мини-апп открыт часами: работать ночью в светлой теме (или
наоборот днём в тёмной) человек не обязан, и переключить это было нельзя.

Проверяется на настоящем бутстрапе темы (node): что свой выбор сильнее
клиента, что «авто» возвращает слушание клиента и что сломанное хранилище
не ломает запуск — тема применяется ДО первой отрисовки, и исключение здесь
означало бы белый экран.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(name: str) -> str:
    m = re.search(r"\n  function " + name + r"\([\s\S]*?\n  }\n", HTML)
    assert m, name
    return m.group(0).replace("\n  ", "\n")


def _run(src: str):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


_STAND = """
var THEME_KEY = 'infragram_theme';
var _LS = {};
var localStorage = { getItem: k => _LS[k] == null ? null : _LS[k],
                     setItem: (k, v) => { _LS[k] = String(v); },
                     removeItem: k => { delete _LS[k]; } };
var tg = { colorScheme: 'light' };
var mq = { matches: true };
function inTelegram() { return true; }
"""


def test_my_choice_beats_the_client_theme():
    out = _run(_STAND + _fn("savedScheme") + _fn("scheme") + r"""
const auto = scheme();                 // клиент светлый — берём светлую
_LS[THEME_KEY] = 'dark';
const mine = scheme();
_LS[THEME_KEY] = 'мусор';              // чужое значение — не тема
const junk = scheme();
delete _LS[THEME_KEY];
console.log(JSON.stringify({auto, mine, junk, back: scheme()}));
""")
    assert out == {"auto": "light", "mine": "dark", "junk": "light", "back": "light"}


def test_a_broken_storage_does_not_break_the_launch():
    """Тема применяется до первой отрисовки: исключение здесь — белый экран."""
    out = _run("""
var THEME_KEY = 'infragram_theme';
var localStorage = { getItem() { throw new Error('заблокировано'); } };
var tg = { colorScheme: 'dark' };
var mq = { matches: false };
function inTelegram() { return true; }
""" + _fn("savedScheme") + _fn("scheme") + r"""
console.log(JSON.stringify({scheme: scheme()}));
""")
    assert out == {"scheme": "dark"}


def test_the_switch_is_on_the_settings_screen():
    assert 'id="set-theme"' in HTML, "переключателя темы нет в настройках"
    assert "function renderThemeRow()" in HTML and "function setAppTheme(" in HTML
    assert "renderThemeRow();" in HTML.split("async function openSettings()")[1][:1200], (
        "настройки открываются, не показав выбранную тему")
    assert "window.__setTheme" in HTML and "window.__themeMode" in HTML
    # Подписи — по-русски, значения — те, что понимает бутстрап.
    for v in ("'auto'", "'light'", "'dark'"):
        assert v in HTML
    assert "Авто" in HTML and "Светлая" in HTML and "Тёмная" in HTML
