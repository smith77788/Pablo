"""«Итоги» — первый блок главного экрана и одна сводка на бот и мини-апп.

Главный экран отвечал на вопрос «как устроен флот», а не «что мне это
принесло». Здесь закреплено, что:
  * блок «Итоги» стоит на главном экране первым и грузится вместе с ним;
  * данные идут из зарегистрированного маршрута, который считает тот же
    `services/results_summary.py`, что и кнопка «📊 Итоги» в боте;
  * экран собран на компонентах дизайн-системы: стили подключены, компоненты
    загружаются раньше экрана, текст из данных экранируется.
"""
from __future__ import annotations

import pathlib
import re

from tests.miniapp_source import api_routes, miniapp_html

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = miniapp_html()
SUMMARY_JS = (ROOT / "mini_app" / "screens" / "summary.js").read_text(encoding="utf-8")
KIT_JS = (ROOT / "mini_app" / "screens" / "ui_kit.js").read_text(encoding="utf-8")


def test_route_registered():
    assert "/api/miniapp/summary" in api_routes()


def test_summary_is_first_on_home():
    home = HTML[HTML.index('<div class="screen show" id="s-home">'):]
    sb = home[home.index('<div class="sb">'):]
    first = re.search(r'<div[^>]*id="([^"]+)"', sb[len('<div class="sb">'):])
    assert first and first.group(1) == "summaryWrap"


def test_home_load_also_loads_summary():
    body = HTML[HTML.index("async function loadHome()"):]
    body = body[: body.index("\n}\n")]
    assert "loadSummary()" in body


def test_design_system_wired():
    assert '<link rel="stylesheet" href="ui_kit.css">' in HTML
    kit = HTML.index('<script src="screens/ui_kit.js"></script>')
    scr = HTML.index('<script src="screens/summary.js"></script>')
    assert kit < scr, "компоненты должны грузиться раньше экрана"
    assert (ROOT / "mini_app" / "ui_kit.css").exists()


def test_screen_reads_summary_endpoint_and_uses_components():
    assert "api('/api/miniapp/summary?days=7')" in SUMMARY_JS
    assert "UI.stats(" in SUMMARY_JS and "UI.alert(" in SUMMARY_JS
    # Ручных стилей на экране нет: вид живёт в ui_kit.css.
    assert 'style="' not in SUMMARY_JS


def test_kit_escapes_and_refuses_code_from_data():
    assert "function esc(s)" in KIT_JS
    # onclick собирается только из имени функции и простых аргументов.
    assert r"/^[A-Za-z_$][\w$]*$/" in KIT_JS
    assert r"/^[a-z0-9_-]{1,40}$/i" in KIT_JS


def test_no_data_is_not_zero():
    """«Нет данных» и ноль — разные ответы: упавшая выборка не рисует «0»."""
    assert "empty ? 'нет данных'" in KIT_JS
