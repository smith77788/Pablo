"""Разрушительные действия из интерфейса обязаны оставлять след.

Массовые операции пишут в operation_audit каждый шаг — по журналу видно, что
делал движок. А удаление аккаунтов, ботов и прокси, сделанное руками из бота
или мини-аппа, не писало НИЧЕГО. На вопрос «куда делись сорок аккаунтов»
ответа в системе не было.

Это не теория: доступ к ресурсам выдаётся ещё и через workspace и через
экосистему, то есть удалить их мог не только хозяин. Без записи о том, кто и
что убрал, разобраться нельзя в принципе.

account_id в этих записях остаётся пустым намеренно: behavioral_engine
считает риск аккаунта как долю неудач среди ВСЕХ его строк в operation_audit
за сутки, и записи интерфейса разбавили бы эту долю — аккаунт выглядел бы
безопаснее, чем он есть. Идентификатор объекта лежит в target.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

from database import db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Pool:
    def __init__(self, result="UPDATE 1"):
        self.result = result
        self.log: list[tuple[str, tuple]] = []

    async def execute(self, sql, *a):
        self.log.append((sql, a))
        return self.result

    async def fetchrow(self, sql, *a):
        self.log.append((sql, a))
        return None


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _audits(pool):
    return [(q, a) for q, a in pool.log if "INSERT INTO operation_audit" in q]


# ── сама запись ─────────────────────────────────────────────────────────────


def test_record_keeps_account_id_empty():
    pool = _Pool()
    _run(db.record_manual_action(pool, 777, "account_delete", target="42"))

    audits = _audits(pool)
    assert audits, "действие не записано"
    sql, args = audits[0]
    assert "account_id" not in sql, (
        "запись интерфейса с account_id разбавит долю неудач в оценке риска "
        "аккаунта — он будет выглядеть безопаснее, чем есть"
    )
    assert 777 in args and "42" in args


def test_record_never_raises():
    """Журнал не должен ронять действие, которое он описывает."""

    class _Broken(_Pool):
        async def execute(self, sql, *a):
            raise RuntimeError("нет связи")

    _run(db.record_manual_action(_Broken(), 777, "account_delete", target="42"))


# ── двери ───────────────────────────────────────────────────────────────────


def test_bot_removal_is_recorded():
    pool = _Pool("UPDATE 1")
    assert _run(db.delete_bot(pool, 555, 777)) is True
    assert _audits(pool), "убрали бота и не записали"


def test_bot_removal_of_a_foreign_bot_is_not_recorded():
    """Ничего не сделали — нечего и записывать."""
    pool = _Pool("UPDATE 0")
    assert _run(db.delete_bot(pool, 555, 777)) is False
    assert not _audits(pool)


def test_proxy_deletion_is_recorded():
    from services.proxy_hygiene import delete_proxy_safely

    pool = _Pool("DELETE 1")
    res = _run(delete_proxy_safely(pool, 777, 5))
    assert res["ok"] is True
    assert _audits(pool), "удалили прокси и не записали"


# ── ни одно удаление аккаунта не проходит мимо журнала ──────────────────────


def _sql_literals(src: str):
    tree = ast.parse(src)
    skip = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = n.body[0] if n.body else None
            if (isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant)
                    and isinstance(d.value.value, str)):
                skip.add(id(d.value))
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            yield "".join(v.value for v in n.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            yield n.value


def test_every_account_deletion_site_records_it():
    offenders = []
    for base in ("bot/handlers", "services", "database"):
        for root, dirs, files in os.walk(os.path.join(_ROOT, base)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                src = open(path, encoding="utf-8").read()
                if not any(re.search(r"DELETE\s+FROM\s+tg_accounts", s, re.I)
                           for s in _sql_literals(src)):
                    continue
                if "record_manual_action" not in src:
                    offenders.append(os.path.relpath(path, _ROOT))
    assert not offenders, (
        "аккаунты удаляются без записи в журнал:\n  " + "\n  ".join(offenders)
        + "\nБез неё на вопрос «куда они делись» ответа нет"
    )
