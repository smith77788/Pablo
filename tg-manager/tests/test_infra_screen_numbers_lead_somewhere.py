"""Экран «Инфраструктура»: четыре числа стали четырьмя списками.

Экран показывал «1234 активных акк · 7 флуд 24ч · 412 операций 24ч · 19 на
прогреве», ниже «Пул europe — 42 акк», ниже журнал — и не нажималось ничего.
Каждое из этих чисел считает конкретный список, который в приложении есть.

Отдельно про пул: среза под пул на сервере не существовало вовсе, поэтому
открыть «те самые 42 аккаунта» было физически нечем. Срез добавлен и обязан
доезжать и до «применить ко всему срезу» — иначе массовая операция ушла бы
шире, чем видел владелец.

И журнал: в operation_audit.action попадает не только тип операции, но и
шесть собственных действий воркера — они вылезали латиницей.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
WORKER = os.path.join(ROOT, "services", "op_worker.py")


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


def _py_func(name: str) -> str:
    src = _api()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(name)


def test_the_four_numbers_are_still_there():
    """Антивакуумность."""
    body = _js_func("openInfra")
    for lbl in ("Активных акк", "Флуд 24ч", "Операций 24ч", "На прогреве"):
        assert lbl in body, f"показатель «{lbl}» пропал"


def test_each_number_opens_its_list():
    body = _js_func("openInfra")
    for target in ("healthGoAccounts('active','')", "openHealth()", "openOps()", "openWarmup()"):
        assert target in body, f"нет перехода через {target}"


def test_pool_row_opens_the_accounts_of_that_pool():
    body = _js_func("openInfra")
    assert "healthGoAccounts('all',''," in body, "строка пула никуда не ведёт"


def test_the_server_actually_has_a_pool_slice():
    """Без серверного среза кнопка на пуле открывала бы весь флот."""
    where = _py_func("_accounts_where")
    assert "acc_pool" in where, "среза по пулу на сервере нет"
    assert "pool = $" in where
    listing = _py_func("accounts")
    assert 'qs.get("pool")' in listing, "список аккаунтов не читает срез по пулу"


def test_select_all_filtered_respects_the_pool():
    """Иначе «применить ко всему срезу» возьмёт аккаунты вне показанного пула."""
    src = _api()
    m = re.search(r'if body\.get\("select_all_filtered"\):(.*?)\n            owned = ', src, re.S)
    assert m, "ветка «весь срез» не найдена"
    assert "acc_pool=" in m.group(1), "массовая операция уходит шире показанного"
    front = _html()
    assert "pool: ACC_POOL_FILTER" in front, "фронт не передаёт пул в «весь срез»"


def test_the_pool_slice_is_visible_and_removable():
    """Молча укоротившийся список выглядит как пропажа аккаунтов."""
    assert "accPoolBanner" in _html(), "срез по пулу ничем не показан"
    clear = _js_func("accPoolClear")
    assert "ACC_POOL_FILTER = ''" in clear and "reloadAccounts()" in clear
    empty_case = _js_func("renderAccounts")
    assert "ACC_POOL_FILTER" in empty_case, "пустой список под срезом зовут «нет аккаунтов»"


def test_audit_actions_are_translated():
    """Шесть собственных действий воркера не покрывались opRu."""
    with open(WORKER, encoding="utf-8") as f:
        w = f.read()
    own = set()
    for m in re.finditer(r'_audit\(\s*\n?\s*pool,\s*\n?\s*[^,]+,\s*\n?\s*([^,\n]+),', w):
        for lit in re.findall(r'"([a-z_]+)"', m.group(1)):
            own.add(lit)
    assert own, "не нашли собственных действий аудита — проверка потеряла смысл"
    m = re.search(r"const AUDIT_RU = \{(.*?)\n\};", _html(), re.S)
    assert m, "словаря действий аудита нет"
    known = set(re.findall(r"^\s{2}(\w+):", m.group(1), re.M))
    assert own <= known, f"без перевода остались: {sorted(own - known)}"
    assert "auditRu(a.action)" in _js_func("openInfra")


def test_empty_states_say_what_would_be_here():
    body = _js_func("openInfra")
    assert "Пулы не заданы" in body and "назначается на карточке аккаунта" in body
    assert "Записей пока нет" in body


def test_error_has_a_way_out():
    assert "errHtml(e.message, 'openInfra()')" in _js_func("openInfra")
