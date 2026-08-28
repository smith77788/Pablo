"""В боте всегда должен быть путь подключить аккаунт — и он должен быть гейтован.

Жалоба владельца: «в меню бота больше нету возможности подключить новые
аккаунты». Причина: кнопки добавления рисовались под условием `total < limit`, а
для плана free лимит аккаунтов равен 0 — значит у любого free-пользователя с уже
привязанными аккаунтами `total < 0` всегда ложно, и кнопки просто исчезали без
единого слова. Снаружи это ровно «подключить нельзя».

Чинится это тем, что кнопки видны ВСЕГДА, а лимит проверяет каждый обработчик
входа и показывает понятный экран апгрейда. Второй инвариант тут не менее важен:
раз кнопка QR теперь видна всегда, обработчик QR ОБЯЗАН проверять лимит — иначе
это обход платного лимита.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(ROOT, "bot", "handlers", "accounts.py")


def _src() -> str:
    with open(ACCOUNTS, encoding="utf-8") as f:
        return f.read()


def _func(src: str, name: str) -> ast.AST:
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} не найдена в accounts.py")


def _func_body_text(src: str, name: str) -> str:
    lines = src.split("\n")
    n = _func(src, name)
    return "\n".join(lines[n.lineno - 1:n.end_lineno])


def test_add_buttons_are_not_hidden_behind_the_limit():
    """Кнопки добавления не должны стоять под `if total < limit`.

    Проверяем структурно: внутри _show_accounts_menu ни один узел `if`, чьё
    условие сравнивает total с limit, не должен оборачивать добавление кнопок
    qr_login/add. Иначе на исчерпанном (или нулевом) лимите путь исчезает.
    """
    src = _src()
    fn = _func(src, "_show_accounts_menu")

    def _mentions_add(node: ast.AST) -> bool:
        for s in ast.walk(node):
            if isinstance(s, ast.Constant) and isinstance(s.value, str) \
                    and ("qr_login" in s.value or s.value == "add"):
                return True
            # callback_data=AccCb(action="qr_login") — строка внутри вызова
        return False

    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        cond = ast.dump(node.test)
        if "total" in cond and "limit" in cond and _mentions_add(node):
            offenders.append(node.lineno)
    assert not offenders, (
        "кнопки добавления снова спрятаны под сравнение лимита "
        f"(строки {offenders}) — на free/исчерпанном лимите путь пропадёт")


def test_qr_login_enforces_the_account_limit():
    """Обработчик QR обязан проверять лимит перед выдачей кода.

    Кнопка QR теперь видна всегда; без этой проверки free-пользователь заводил
    бы аккаунты в обход платного лимита. Гейт тот же, что у входа по номеру:
    limit==0 и len(accounts) >= limit.
    """
    body = _func_body_text(_src(), "cb_qr_login")
    # проверка ДО фактического старта QR
    i_limit = body.find("_get_account_limit")
    i_start = body.find("start_qr_login")
    assert i_limit != -1, "cb_qr_login не запрашивает лимит аккаунтов"
    assert i_start != -1, "cb_qr_login не запускает QR — тест смотрит не туда"
    assert i_limit < i_start, (
        "лимит проверяется ПОСЛЕ старта QR — гейт бесполезен")
    assert "limit == 0" in body, "нет ветки для плана без аккаунтов (limit==0)"
    assert "subscription_locked_markup" in body, (
        "нет экрана апгрейда — пользователь упрётся без объяснения")


def test_add_and_qr_gate_the_same_way():
    """QR и вход по номеру гейтуются одинаково — иначе один из путей дыра."""
    src = _src()
    add = _func_body_text(src, "cb_add_account")
    qr = _func_body_text(src, "cb_qr_login")
    for marker in ("_get_account_limit", "limit == 0",
                   "len(accounts) >= limit", "subscription_locked_markup"):
        assert marker in add, f"cb_add_account потерял гейт: {marker}"
        assert marker in qr, f"cb_qr_login не повторяет гейт входа по номеру: {marker}"
