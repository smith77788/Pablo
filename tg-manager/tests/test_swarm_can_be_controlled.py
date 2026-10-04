"""Рой ботов: его наконец можно включать из мини-аппа, а не только смотреть.

Экран показывал у каждого бота «🟢 В рою / ⚫ Выключен», его роль и метрики —
и прямо отсылал владельца наружу: «Рой включается на карточке бота в
Telegram-боте». Так и было: переключателя `swarm_enabled` у мини-аппа не
существовало, он жил только в боте (`bot/handlers/swarm.py`). Роль же меняется
маршрутом PUT /bot/{id}/role, который есть с самого начала и которым на этом
экране не пользовался никто.

Заодно последний экран из списка «только числа» — итоги A/B-теста: пустое
состояние и ошибка не давали ни одной кнопки.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _api() -> str:
    with open(API, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(name)


def _retries(body: str, screen: str) -> bool:
    """errHtml(..., 'openX()') — аргумент сообщения может быть любым выражением
    (например errRu(e)), поэтому ищем вызов со скобками, а не точную строку."""
    for m in re.finditer(r"errHtml\(", body):
        depth, j = 0, m.end() - 1
        while j < len(body):
            if body[j] == "(":
                depth += 1
            elif body[j] == ")":
                depth -= 1
                if depth == 0:
                    if f"'{screen}()'" in body[m.end():j]:
                        return True
                    break
            j += 1
    return False


def _py_func(name: str) -> str:
    src = _api()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(name)


def test_the_screen_still_shows_the_swarm():
    """Антивакуумность."""
    body = _js_func("openSwarm")
    assert "swarm_enabled" in body and "/api/miniapp/swarm" in body


def test_there_is_a_writer_for_swarm_membership():
    assert 'add_post("/api/miniapp/bot/{bot_id}/swarm", set_bot_swarm_api)' in _api(), (
        "включить бота в рой из мини-аппа по-прежнему нечем"
    )


def test_the_writer_is_owner_scoped_and_explicit():
    body = _py_func("set_bot_swarm_api")
    assert "_get_uid(request)" in body and "401" in body
    assert "added_by=$3" in body, "можно переключить чужого бота"
    assert '"enabled" not in body' in body, "пустое тело молча что-то меняет"
    assert "max_val=2**63 - 1" in body, "id бота обрезается по 2**31"
    assert '"UPDATE 0"' in body, "несуществующий бот отвечает успехом"


def test_row_can_be_toggled_and_its_role_changed():
    body = _js_func("openSwarm")
    assert "swarmToggle(" in body and "swarmRole(" in body
    tog = _js_func("swarmToggle")
    assert "/swarm'" in tog.replace('"', "'") and "enabled" in tog
    role = _js_func("swarmRole")
    assert "askChoice" in role and "/role'" in role.replace('"', "'")
    assert "method:'PUT'" in role.replace('"', "'")


def test_role_choices_match_what_the_server_accepts():
    """Роль не из списка сервер отвергнет, и кнопка будет врать."""
    handler = _py_func("set_bot_role_api")
    m = re.search(r'valid_roles = \((.*?)\)', handler, re.S)
    assert m, "список ролей в маршруте не найден"
    valid = set(re.findall(r"'(\w+)'|\"(\w+)\"", m.group(1)))
    valid = {a or b for a, b in valid}
    offered = set(re.findall(r"value:'(\w+)'", _js_func("swarmRole")))
    assert offered, "экран не предлагает ролей"
    assert offered <= valid, f"сервер не примет: {sorted(offered - valid)}"


def test_screen_no_longer_sends_the_owner_to_the_bot():
    h = _html()
    assert "Рой включается на карточке бота в Telegram-боте" not in h, (
        "экран всё ещё отсылает владельца делать это в другом месте"
    )


def test_empty_and_error_states_lead_somewhere():
    body = _js_func("openSwarm")
    assert "goTab('bots')" in body, "пустой рой — тупик"
    assert _retries(body, "openSwarm"), "у ошибки роя нет работающего Повторить"
    ab = _js_func("openAbResults")
    assert "goTab('broadcasts')" in ab, "пустые итоги A/B — тупик"
    assert _retries(ab, "openAbResults"), "у ошибки итогов A/B нет Повторить"


def test_role_is_not_shown_raw_when_unknown():
    body = _js_func("openSwarm")
    assert "esc(r.bot_role" in body, "неизвестная роль попадает на экран без экранирования"
