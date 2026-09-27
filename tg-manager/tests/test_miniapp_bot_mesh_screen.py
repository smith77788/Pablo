"""Задачи между ботами видны владельцу, а не только таблице.

Ядро Bot Mesh (`services/bot_mesh.py`, схема v212) писало задачи и трассу
передач в базу, но экрана у этого не было: посмотреть, где задача сейчас и
почему брошена, было негде — «цепочка работает» приходилось принимать на
веру. Это ровно та скрытая неполнота, которую стандарт владельца запрещает:
данные есть, показать их нечем.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "services", "mini_app_api.py")
HTML = os.path.join(ROOT, "mini_app", "index.html")


@functools.lru_cache(maxsize=1)
def _api_src() -> str:
    return open(API, encoding="utf-8").read()


@functools.lru_cache(maxsize=1)
def _html() -> str:
    return open(HTML, encoding="utf-8").read()


def _api_func(name: str) -> str:
    src = _api_src()
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"обработчик {name} не найден")


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
    raise AssertionError(f"не найден конец функции {name}")


def test_routes_are_registered_and_owner_scoped():
    src = _api_src()
    assert '"/api/miniapp/mesh/tasks", mesh_tasks' in src, "маршрут списка не зарегистрирован"
    assert '"/api/miniapp/mesh/task/{task_id}", mesh_task_detail' in src, (
        "маршрут трассы не зарегистрирован")
    for name in ("mesh_tasks", "mesh_task_detail"):
        body = _api_func(name)
        assert "_get_uid(request)" in body, f"{name} не проверяет пользователя"
        assert "owner_id=$1" in body or "owner_id=$2" in body, (
            f"{name} читает чужие задачи: нет ограничения по владельцу")


def test_list_is_paged_and_honest():
    body = _api_func("mesh_tasks")
    assert "_list_limit(" in body, "потолок списка задач не подвинуть"
    assert "COUNT(*)" in body and '"total"' in body, (
        "список задач не несёт настоящий итог — страница выдаст себя за всё")
    js = _js_func("openBotMesh")
    assert "moreBtn(" in js, "на экране задач нет кнопки «Показать ещё»"
    assert "limit=" in js, "экран не передаёт потолок на сервер"


def test_screen_names_bots_not_numbers():
    """Идентификатор бота владельцу ничего не говорит — нужны имена."""
    body = _api_func("mesh_tasks")
    assert "_mesh_bot_names(" in body, "имена ботов не подмешиваются"
    names = _api_func("_mesh_bot_names")
    assert "managed_bots" in names and "added_by=$1" in names, (
        "имена ботов берутся не из ботов владельца")
    js = _js_func("openBotMesh")
    assert "bot_name" in js, "экран рисует идентификаторы вместо имён"


def test_drop_reason_is_explained_in_russian():
    """«max_depth» — это код предохранителя, а не объяснение владельцу."""
    h = _html()
    assert "const MESH_DROP" in h, "словаря причин отбрасывания нет"
    m = re.search(r"const MESH_DROP\s*=\s*\{(.*?)\n\};", h, re.S)
    assert m, "словарь причин не разобрать"
    table = m.group(1)
    for code in ("expired", "max_depth", "loop", "duplicate"):
        assert code in table, f"причина {code} не объяснена по-русски"
    assert re.search(r"[а-яё]", table, re.I), "объяснения не на русском"
    # Незнакомый код показываем как есть, а не прячем.
    assert "MESH_DROP[r] || r" in h, (
        "неизвестная причина будет проглочена вместо показа")


def test_both_screens_have_a_way_out():
    """Экран без выхода — отдельный класс дефекта в этом продукте."""
    h = _html()
    for sid in ("s-botmesh", "s-botmeshdetail"):
        i = h.find(f'id="{sid}"')
        assert i > 0, f"экран {sid} не найден"
        head = h[i:i + 700]
        assert 'class="back"' in head, f"на экране {sid} нет кнопки «назад»"
    # Упавший запрос предлагает повтор, а не тупик.
    assert "errHtml(e.message, 'openBotMesh()')" in h, "список без повтора при ошибке"
    assert "errHtml(e.message, 'retryBotMeshTask()')" in h, "трасса без повтора при ошибке"
    assert re.search(r"function\s+retryBotMeshTask\s*\(", h), (
        "повтор трассы зовёт несуществующую функцию")


def test_entry_point_exists():
    """Экран, до которого нельзя дойти, — то же самое, что его нет."""
    h = _html()
    assert 'onclick="openBotMesh()"' in h, "плитки, ведущей на экран задач, нет"
