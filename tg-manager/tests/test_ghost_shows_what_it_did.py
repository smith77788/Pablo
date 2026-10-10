"""Призрачный движок: видно, что призрак делал, и настройки можно менять.

Экран «👻 Призрачный движок» показывал список профилей с настройками и двумя
кнопками — «Вкл/Выкл» и «Удалить». Журнал действий (ghost_action_log) движок
вёл с самого начала, но экран его не показывал: проверить, работает ли призрак
и что именно он сделал, было нельзя. Настройки нельзя было поменять — только
удалить профиль и создать заново.

Отдельно тест держит проверку диапазонов: часы и потолок уходили в базу через
голый int() без границ, и «часы 99» или «1000 действий в день» сохранялись
молча, после чего движок просто не находил окна активности.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


# ── маршруты и скоуп ─────────────────────────────────────────────────────────

def test_detail_and_update_routes_exist():
    assert "GET /api/miniapp/ghost/{profile_id}" in SNAPSHOT
    assert "PUT /api/miniapp/ghost/{profile_id}" in SNAPSHOT


def test_detail_is_owner_scoped_and_returns_the_log():
    h = _handler("ghost_detail")
    assert "gp.owner_id=$2" in h, "чужой профиль открывается по номеру"
    assert "ghost_action_log" in h, "журнал действий не отдаётся"
    assert "today_actions" in h, "не видно, сколько сделано сегодня"
    assert "_GHOST_ACTION_RU" in h, "действия уходят на экран служебными ключами"


def test_update_is_owner_scoped():
    h = _handler("ghost_update")
    assert "WHERE id=$1 AND owner_id=$2" in h, "правка профиля без скоупа владельца"
    assert "_ghost_fields(" in h


def test_settings_are_range_checked():
    m = re.search(r"def _ghost_fields\(data: dict\).*?return \{\"personality\"", API, re.S)
    assert m, "нет разбора настроек призрака"
    src = m.group(0)
    assert "min_val=0, max_val=23" in src, "часы активности без границ 0–23"
    assert "max_val=200" in src, "потолок действий в день без границы"
    assert "max_val=1440" in src, "пауза между действиями без границы"
    assert "start == end" in src, "пустое окно активности проходит молча"


def test_create_uses_the_same_validation():
    h = _handler("ghost_create")
    assert "_ghost_fields(data)" in h, "создание профиля минует проверку диапазонов"
    assert "int(data.get(\"active_hours_start\"" not in h, \
        "остался голый int() без границ"


# ── экран ────────────────────────────────────────────────────────────────────

def test_profile_row_opens_the_detail():
    body = _fn("openGhost")
    assert "openGhostDetail(" in body, "профиль нельзя открыть — только включить и удалить"
    assert 'id="s-ghostdetail"' in HTML


def test_detail_shows_progress_and_the_log():
    body = _fn("openGhostDetail")
    assert "today_actions" in body, "не видно, сколько призрак сделал сегодня"
    assert "d.log" in body, "журнал действий не показывается"
    assert "action_ru" in body, "действия показываются служебными ключами"
    assert "editGhost(" in body and "toggleGhost(" in body


def test_detail_explains_an_empty_log_differently_when_disabled():
    body = _fn("openGhostDetail")
    assert "p.enabled" in body and "empty(" in body, \
        "пустой журнал не объясняет, выключен профиль или просто не настало окно"


def test_settings_can_be_changed_without_recreating():
    assert "async function editGhost(" in HTML
    body = _fn("editGhost")
    assert "method:'PUT'" in body
    assert "personality" in body and "daily_cap" in body


def test_empty_and_error_states_have_a_way_out():
    body = _fn("openGhost")
    assert "openGhostModal()" in body, "«Нет профилей» без кнопки создания"
    assert re.search(r"errHtml\(errRu\(e\)\s*,", body), "ошибка списка без «Повторить»"
    assert re.search(r"errHtml\(errRu\(e\)\s*,", _fn("openGhostDetail")), \
        "ошибка профиля без «Повторить»"


def test_ask_prompt_supports_a_numeric_keyboard():
    body = _fn("askPrompt")
    assert "opts.type" in body, "числовые поля открывают буквенную клавиатуру"
    assert "'number'" in body
