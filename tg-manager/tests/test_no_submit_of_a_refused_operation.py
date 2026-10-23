"""Операция, которую исполнитель отвергает всегда, не должна ставиться.

ЗАЧЕМ. `_exec_bulk_edit_channels` отключили: он брал одно название и ставил
его всем каналам в диалогах каждого выбранного аккаунта — необратимо, имя
видят подписчики. Исполнитель заменили на безусловный отказ, и это верно: уже
стоящие в очереди операции должны завершиться понятным отказом. Но кнопка в
боте продолжала ставить именно эту операцию: владелец читал «✅ Редактирование
каналов запущено», а через минуту получал отказ в /ops.

ЧТО ДЕРЖИТ. Если исполнитель операции — безусловный отказ (тело функции
сводится к `return {"status": "failed", ...}`), то ставить такую операцию не
имеет права никто. Исполнитель при этом остаётся: он доводит до отказа то, что
уже в очереди.
"""
from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WORKER = _ROOT / "services" / "op_worker.py"


def _dispatch() -> dict[str, str]:
    """op_type → имя функции-исполнителя из карты диспетчера."""
    src = _WORKER.read_text(encoding="utf-8")
    return dict(re.findall(r'"([a-z0-9_]+)":\s*(_exec_[a-z0-9_]+),', src))


def _always_refuses(fn_name: str) -> bool:
    """Тело исполнителя — только возврат отказа (docstring не считается)."""
    src = _WORKER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name):
            continue
        body = [st for st in node.body
                if not (isinstance(st, ast.Expr)
                        and isinstance(st.value, ast.Constant)
                        and isinstance(st.value.value, str))]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return False
        ret = body[0].value
        if not isinstance(ret, ast.Dict):
            return False
        for key, value in zip(ret.keys, ret.values):
            if (isinstance(key, ast.Constant) and key.value == "status"
                    and isinstance(value, ast.Constant)
                    and value.value == "failed"):
                return True
        return False
    return False


def _submit_sites() -> dict[str, list[str]]:
    """op_type → места постановки (файл:строка)."""
    out: dict[str, list[str]] = {}
    for base in ("bot", "services", "database"):
        for path in sorted((_ROOT / base).rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "submit(" not in src:
                continue
            rel = path.relative_to(_ROOT).as_posix()
            for m in re.finditer(r"submit\s*\(", src):
                i, depth = m.end(), 1
                while i < len(src) and depth:
                    depth += (src[i] == "(") - (src[i] == ")")
                    i += 1
                call = src[m.start():i]
                line = src[:m.start()].count("\n") + 1
                for op in re.findall(r"[\"']([a-z0-9_]+)[\"']\s*,", call):
                    out.setdefault(op, []).append(f"{rel}:{line}")
    return out


def test_the_measurer_sees_both_sides():
    """Антивакуумность: без карты диспетчера и точек постановки проверка пуста."""
    assert len(_dispatch()) >= 50, f"карта диспетчера прочитана плохо: {len(_dispatch())}"
    assert len(_submit_sites()) >= 20, "точки постановки операций не найдены"
    assert _always_refuses("_exec_bulk_edit_channels"), (
        "детектор не узнаёт заведомый отказ — _exec_bulk_edit_channels именно "
        "такой (если его вернули к работе, обновите этот пример)")


def test_a_refused_operation_is_not_submitted_anywhere():
    refused = {op: fn for op, fn in _dispatch().items() if _always_refuses(fn)}
    assert refused, "в диспетчере нет ни одного отключённого исполнителя — пример для проверки пропал"
    sites = _submit_sites()
    bad = []
    for op, fn in sorted(refused.items()):
        where = sites.get(op) or []
        if where:
            bad.append(f"  {op} ({fn} всегда отказывает) ставится в: "
                       + ", ".join(sorted(set(where))))
    assert not bad, (
        "операция ставится, хотя исполнитель отвергает её всегда — владелец "
        "прочитает «запущено» и получит отказ:\n" + "\n".join(bad))
