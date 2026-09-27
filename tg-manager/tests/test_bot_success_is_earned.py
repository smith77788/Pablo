"""Бот не объявляет успех, пока запись не подтверждена.

Разобранный случай: обработчик оборачивает запись в try, ошибку кладёт в лог
(`log_exc_swallow`) — и следом безусловно печатает «✅ удалено». Пользователь
читает, что дело сделано, уходит с экрана, а в базе всё на месте.

* удаление шага воронки — шаг оставался и продолжал писать подписчикам;
* удаление кластера — кластер оставался в списке;
* очистка состава экосистемы — состав оставался;
* удаление мёртвых аккаунтов — в отчёте стояло «Удалено 7», даже если все
  семь запросов упали: число бралось из списка кандидатов, а не из
  выполненных удалений.

Общий ратчет тут не годится: «✅» в боте стоит и в подписях кнопок, и в
словарях статусов, и в списках с галочками — измеритель даёт десятки ложных
находок. Поэтому проверка адресная: у разобранных обработчиков должен быть и
отказ, и условие вокруг успеха.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

CASES = [
    ("bot/handlers/funnels.py", "cb_fn_step_delete"),
    ("bot/handlers/cluster_manager.py", "cb_cluster_delete"),
    ("bot/handlers/ecosystems.py", "cb_eco_members_clear_do"),
    ("bot/handlers/accounts.py", "cb_del_dead_accounts_do"),
]

_SUCCESS = re.compile(r"(✅|🗑)")
_FAILURE = re.compile(r"(❌|⚠️)")
_DML = re.compile(r"(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)", re.I)


def _func(rel: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel}: обработчик {name} не найден — его переименовали или убрали")


def _strings(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append(
                "".join(v.value for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            )
    return out


def test_cases_still_write_to_the_database():
    """Если обработчик перестал писать в базу, проверка ниже потеряла смысл —
    ловим это прямо, а не тихо зеленеем."""
    for rel, name in CASES:
        body = "\n".join(_strings(_func(rel, name)))
        assert _DML.search(body), f"{rel}:{name} больше не пишет в базу — проверьте, актуальна ли проверка"


def test_handler_has_a_failure_branch():
    for rel, name in CASES:
        texts = _strings(_func(rel, name))
        assert any(_SUCCESS.search(t) for t in texts), f"{rel}:{name} — нет сообщения об успехе"
        assert any(_FAILURE.search(t) and re.search(r"[Нн]е удалось|остал", t) for t in texts), (
            f"{rel}:{name} — у обработчика нет ответа на случай, когда запись не прошла; "
            "значит, при ошибке он снова скажет «готово»")


def test_success_message_is_conditional():
    """Сообщение об успехе должно лежать внутри ветвления: безусловное «✅»
    печатается и при упавшем запросе."""
    for rel, name in CASES:
        fn = _func(rel, name)
        ok_nodes = [
            n for n in ast.walk(fn)
            if isinstance(n, (ast.Constant, ast.JoinedStr))
            and any(_SUCCESS.search(t) for t in _strings(n))
        ]
        assert ok_nodes, f"{rel}:{name} — не нашёл сообщение об успехе"
        # родительские узлы: успех должен быть внутри If (ветка или тернарник)
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(fn):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        def under_if(node) -> bool:
            cur = node
            while id(cur) in parents:
                cur = parents[id(cur)]
                if isinstance(cur, (ast.If, ast.IfExp)):
                    return True
            return False
        # либо успех внутри ветвления, либо перед ним стоит ранний выход по
        # неудаче (guard): `if not cleared: ... return`
        guards = [
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and any(isinstance(st, ast.Return) for st in ast.walk(ast.Module(body=n.body, type_ignores=[])))
        ]
        def guarded(node) -> bool:
            return any(g < node.lineno for g in guards)
        assert any(under_if(n) or guarded(n) for n in ok_nodes), (
            f"{rel}:{name} — «✅» печатается безусловно, вне всякой проверки результата")
