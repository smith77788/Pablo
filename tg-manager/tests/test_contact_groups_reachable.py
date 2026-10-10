"""Группы контактов доступны из интерфейса и заперты по владельцу.

Сервер умел группы целиком — создать, переименовать, удалить, набрать состав
(`/uch/groups` GET/POST/PUT/DELETE, `/uch/bulk/group`), а репозиторий умел
фильтр `group_id`. С фронта не звали НИ ОДИН из этих маршрутов, и сам список
контактов параметр `group_id` не читал: открыть группу было физически некуда.

Заодно закрыты две дыры, которые вскрылись при подключении экрана:
* `bulk_add_to_group` принимала owner_id и не использовала его — по чужому id
  можно было набить чужую группу;
* `delete_group` сначала чистила состав `WHERE group_id=$1` без проверки
  владельца — чужую группу можно было опустошить по одному id.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
ENGINE = os.path.join(ROOT, "services", "contacts_hub", "bulk_ops_engine.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _engine() -> str:
    with open(ENGINE, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def _py_func(name: str) -> str:
    src = _engine()
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def test_there_is_a_way_into_groups():
    h = _html()
    assert 'onclick="openContactGroups()"' in h, "в группы нельзя войти ни с одного экрана"
    assert 'id="s-contactgroups"' in h, "экрана групп нет"


def test_groups_can_be_created_renamed_deleted():
    assert "/api/miniapp/uch/groups" in _js_func("cgCreate")
    ren = _js_func("cgRename")
    assert "PUT" in ren and "/api/miniapp/uch/groups/" in ren
    dele = _js_func("cgDelete")
    assert "DELETE" in dele
    assert "confirmDelete" in dele, "группа удаляется без подтверждения"


def test_group_filter_reaches_the_list():
    """Открыть группу = увидеть её контакты. Иначе группа — пустая витрина."""
    body = _js_func("loadContacts")
    assert "group_id" in body, "список контактов не умеет фильтр по группе"
    src = open(API, encoding="utf-8").read()
    i = src.find("async def uch_contacts(")
    assert i > 0
    tail = src[i:i + 2500]
    assert "group_id" in tail, "маршрут списка не читает group_id"
    assert "group_id=group_id" in tail, "прочитал, но не передал в репозиторий"


def test_active_group_filter_is_visible():
    """Молчаливый фильтр читается как «контактов стало меньше»."""
    assert "contactGroupBar" in _html(), "полосы активного фильтра нет"
    assert "cgRenderBanner();" in _js_func("loadContacts"), (
        "полоса не перерисовывается при загрузке списка")
    assert "cgClearGroup" in _html(), "фильтр по группе нечем снять"


def test_bulk_add_to_group_exists_and_is_honest():
    body = _js_func("bulkGroupSelected")
    assert "/api/miniapp/uch/bulk/group" in body
    assert "askChoice" in body, "группу выбирают из списка, а не вводят номером"
    assert "уже был" in body, "ноль добавленных читается как «кнопка не сработала»"


def test_group_writes_are_scoped_to_the_owner():
    add = _py_func("bulk_add_to_group")
    assert "owner_id" in add.split("async def")[0] + add
    assert "g.owner_id = $2" in add or "g.owner_id=$2" in add, (
        "добавление в группу не проверяет владельца группы")
    assert "c.owner_id = g.owner_id" in add, "в группу можно занести чужой контакт"

    rem = _py_func("bulk_remove_from_group")
    assert "g.owner_id" in rem, "удаление из группы не проверяет владельца"

    dele = _py_func("delete_group")
    assert "contact_group_members" not in dele, (
        "состав чистится отдельным запросом — он и был без проверки владельца; "
        "внешний ключ и так стоит ON DELETE CASCADE")
    assert "owner_id=$2" in dele.replace(" ", "")


def test_group_counters_count_rows_not_attempts():
    add = _py_func("bulk_add_to_group")
    assert "RETURNING 1" in add and "len(rows)" in add, (
        "счётчик добавленных считает попытки, а не строки")
    rem = _py_func("bulk_remove_from_group")
    assert "RETURNING 1" in rem and "len(rows)" in rem


def test_askchoice_helper_exists():
    """Антивакуумность: без общего диалога выбора проверка выше ничего не значит."""
    h = _html()
    assert "function askChoice" in h
    body = _js_func("askChoice")
    assert "Отмена" in body, "из диалога выбора нет выхода"
