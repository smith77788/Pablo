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


@functools.lru_cache(maxsize=1)
def _api() -> str:
    # весь API-слой: ручки контактов вынесены в services/mini_app_uch.py
    from tests.miniapp_source import api_source

    return api_source()


def _api_func(name: str) -> str:
    """Тело хендлера mini_app_api по ТОЧНЫМ границам функции (ast), а не по срезу
    фиксированной длины: хендлер растёт, и окно `src[i:i+N]` рано или поздно
    промахивается мимо нужной строки — защита тогда гаснет молча (ровно это и
    случилось с проверкой group_id)."""
    src = _api()
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"функция {name} не найдена в API")


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
    fn = _api_func("uch_contacts")
    assert "group_id" in fn, "маршрут списка не читает group_id"
    assert "group_id=group_id" in fn, "прочитал, но не передал в репозиторий"


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


def test_the_card_can_change_the_groups_of_one_contact():
    """Состав группы менялся ТОЛЬКО массовым действием над выделением.

    Чтобы переложить одного человека, владелец уходил из его карточки в
    список, включал режим выбора, снова искал там этот же контакт и отмечал
    его. В API ручки на один контакт были
    (POST/DELETE /uch/contacts/{id}/groups/{gid}), но их не звал никто — и
    владельца в их запросах не было вообще, то есть по перебору целого id
    группы можно было набить чужую группу. Они удалены, а карточка зовёт тот
    же массовый путь списком из одного контакта.
    """
    assert "cdEditGroups()" in _html(), "в карточке нет входа в группы контакта"
    body = _js_func("cdEditGroups")
    assert "/api/miniapp/uch/bulk/group" in body, (
        "карточка зовёт не тот путь, у которого проверен владелец")
    assert "contact_ids:[id]" in body.replace(" ", ""), (
        "в массовую ручку уходит не этот контакт")
    assert "'remove'" in body and "'add'" in body, (
        "из группы нельзя убрать — только добавить")
    assert "и так не был" in body or "уже в этой группе" in body, (
        "ноль изменённых строк читается как «кнопка не сработала»")
    assert "openContactDetail(id)" in body, (
        "карточка не перечитана: галочка осталась бы прежней")


def test_there_is_one_implementation_of_group_membership():
    """Вторая реализация той же операции — это вторая копия той же дыры.

    repository.add_contact_to_group / remove_contact_from_group делали ту же
    работу без владельца в запросе. Возврат любой из них вернёт и дыру:
    починили бы снова только одну.
    """
    import services.contacts_hub.repository as repo

    for name in ("add_contact_to_group", "remove_contact_from_group"):
        assert not hasattr(repo, name), (
            f"{name} вернулась в репозиторий: состав группы снова меняют два "
            "разных запроса, и владельца проверяет только один")
    uch = os.path.join(ROOT, "services", "mini_app_uch.py")
    with open(uch, encoding="utf-8") as f:
        src = f.read()
    # Именно состав у одного контакта: /uch/groups/{group_id} — это сама
    # группа (переименовать, удалить), она жива и нужна.
    assert "contacts/{contact_id}/groups/" not in src, (
        "ручки состава группы на один контакт снова зарегистрированы")
