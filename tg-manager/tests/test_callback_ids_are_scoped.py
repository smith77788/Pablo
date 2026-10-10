"""id из кнопки — это ввод клиента, а не доказательство владения.

callback_data полностью контролируется тем, кто нажимает: у каждого
пользователя этого продукта есть свой MTProto-клиент, и отправить кнопку с
любым account_id, bot_id или funnel_id он может напрямую, не открывая
интерфейс.

Экраны подтверждений и деталей искали аккаунт запросом `WHERE id=$1` без
владельца — и показывали имя и ТЕЛЕФОН чужого аккаунта. Само действие дальше
отбивалось (там скоуп был), так что утекали именно личные данные: телефон,
username, журнал разогрева, телеметрия риска, выручка чужого A/B-эксперимента
в Stars.

Хуже было в двух местах: удаление шага автоворонки и цели content mesh
выполнялось ДО проверки владения — проверка стояла ниже, чтобы нарисовать
экран. Подставив чужой funnel_id/mesh_id, можно было удалить чужую строку и
получить в ответ вежливое «не найдена».

Тест держит оба правила: подпись аккаунта берётся только через общую
функцию со скоупом, а разрушительное действие идёт после проверки.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    return open(os.path.join(_ROOT, rel), encoding="utf-8").read()


def _fn(rel: str, name: str):
    for n in ast.walk(ast.parse(_src(rel))):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{rel}: {name} не найдена — тест устарел")


def _sql_literals(src: str) -> list[str]:
    tree = ast.parse(src)
    skip = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = n.body[0] if n.body else None
            if (isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant)
                    and isinstance(d.value.value, str)):
                skip.add(id(d.value))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            out.append("".join(v.value for v in n.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            out.append(n.value)
    return out


# ── Общая дверь: подпись аккаунта ───────────────────────────────────────────


class _Pool:
    def __init__(self, row=None):
        self.row, self.sql = row, []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        return self.row


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_label_query_is_scoped_by_owner():
    from database import db

    pool = _Pool({"first_name": "Аня", "phone": "+79990000000"})
    label = _run(db.get_account_label(pool, 5, 777))

    assert label == "Аня"
    assert "owner_id" in pool.sql[0], "подпись аккаунта берётся без владельца"


def test_foreign_account_has_no_label():
    from database import db

    assert _run(db.get_account_label(_Pool(None), 5, 777)) is None, (
        "чужой аккаунт не должен получать подпись — иначе телефон уйдёт на экран"
    )


# ── Ни один хендлер бота не тянет аккаунт по одному id ──────────────────────


def test_no_handler_reads_an_account_by_bare_id():
    offenders = []
    base = os.path.join(_ROOT, "bot", "handlers")
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            for sql in _sql_literals(open(path, encoding="utf-8").read()):
                if not re.search(r"FROM\s+tg_accounts", sql, re.I):
                    continue
                if not re.search(r"\bid\s*=\s*\$1", sql):
                    continue
                low = sql.lower()
                if "owner_id" in low or "owner" in low:
                    continue
                offenders.append((os.path.relpath(path, _ROOT), " ".join(sql.split())[:90]))
    assert not offenders, (
        "аккаунт читается по одному id из кнопки — это чужой телефон на экране:\n"
        + "\n".join(f"  {p}: {s}" for p, s in offenders)
    )


@pytest.mark.parametrize("rel,name", [
    ("bot/handlers/account_cleaner.py", "cb_confirm_del_contacts"),
    ("bot/handlers/account_cleaner.py", "cb_do_leave_all"),
    ("bot/handlers/account_warmup.py", "cb_warmup_select_plan"),
    ("bot/handlers/account_warmup.py", "cb_warmup_plan_log"),
    ("bot/handlers/physics_hub.py", "cb_physics_detail"),
])
def test_account_screens_use_the_shared_label(rel, name):
    body = ast.unparse(_fn(rel, name))
    assert "get_account_label" in body, (
        f"{rel}:{name} строит подпись аккаунта сам, мимо проверки владельца"
    )


# ── Остальные экраны деталей ────────────────────────────────────────────────


def test_stars_experiment_is_owner_scoped():
    body = ast.unparse(_fn("bot/handlers/stars_hub.py", "cb_detail"))
    assert "stars_experiments" in body
    assert "owner_id" in body, (
        "чужой A/B-эксперимент открывался целиком, вместе с выручкой в Stars"
    )


def test_bot_username_lookup_is_owner_scoped():
    body = ast.unparse(_fn("bot/handlers/presence_pack.py", "cb_pack_pick_bot"))
    assert "added_by" in body, "username чужого бота читался по одному bot_id"


# ── Разрушительное действие — после проверки ────────────────────────────────


@pytest.mark.parametrize("rel,name,guard,table", [
    ("bot/handlers/auto_funnel_hub.py", "cb_af_del_step", "_get_funnel", "auto_funnel_steps"),
    ("bot/handlers/content_mesh_hub.py", "cb_mesh_del_target", "_get_mesh", "mesh_targets"),
])
def test_ownership_is_checked_before_delete(rel, name, guard, table):
    body = ast.unparse(_fn(rel, name))
    i_guard = body.find(guard)
    i_del = body.find(f"DELETE FROM {table}")
    assert i_guard != -1, f"{rel}:{name} не проверяет владение вовсе"
    assert i_del != -1, "DELETE не найден — тест устарел"
    assert i_guard < i_del, (
        f"{rel}:{name} удаляет строку до проверки владения: по чужому id она "
        "будет удалена, а пользователь увидит «не найдена»"
    )
