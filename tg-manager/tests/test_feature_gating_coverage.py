"""Guard: каждый фича-гейт реально закрывает платную функцию.

Механизм гейтинга в проекте — вызовы ``require_plan(pool, uid, "<plan>")`` и
``require_feature(pool, uid, "<feature_key>")`` в хендлерах. Исторически строка
плана пишется по-разному ("starter"/"pro"/"enterprise"/"paid") — все они через
PLAN_ALIASES означают один тариф ``paid``.

Тихий, но опасный класс багов (абуз + расхождение): опечатка в строке плана
(``"startr"``) или гейт с ``"free"`` → ``coerce_plan`` вернёт ``free`` →
``require_plan`` пропустит ВСЕХ → платная фича утечёт бесплатно. Этот тест
статически сканирует все вызовы гейтов и падает, если такое появится.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from bot.utils import tariffs

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Соберём module-level строковые константы (_PRO = "pro" и т.п.)."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _resolve(arg: ast.expr, consts: dict[str, str]) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _iter_gate_calls():
    """Отдаёт (файл, строка, func_name, plan_or_feature_arg)."""
    for path in BOT_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        consts = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            if name not in ("require_plan", "require_feature"):
                continue
            # require_plan(pool, user_id, min_plan) — 3-й позиционный или kw min_plan
            # require_feature(pool, user_id, feature_key) — 3-й позиционный или kw feature_key
            arg = None
            if len(node.args) >= 3:
                arg = node.args[2]
            else:
                for kw in node.keywords:
                    if kw.arg in ("min_plan", "feature_key", "feature", "plan"):
                        arg = kw.value
            if arg is None:
                continue
            yield path.relative_to(BOT_DIR.parent), node.lineno, name, _resolve(arg, consts)


def test_every_require_plan_gate_demands_the_paid_tier():
    offenders = []
    for relpath, lineno, name, value in _iter_gate_calls():
        if name != "require_plan" or value is None:
            continue  # динамические аргументы не проверяем статически
        if tariffs.coerce_plan(value) != "paid":
            offenders.append(f"{relpath}:{lineno}: require_plan(..., {value!r}) → {tariffs.coerce_plan(value)!r}")
    assert not offenders, (
        "Гейт не требует платного тарифа (опечатка/`free` → утечка платной фичи):\n"
        + "\n".join(offenders)
    )


def test_every_require_feature_key_is_known():
    known = set(tariffs.feature_keys())
    offenders = []
    for relpath, lineno, name, value in _iter_gate_calls():
        if name != "require_feature" or value is None:
            continue
        if value not in known:
            offenders.append(f"{relpath}:{lineno}: require_feature(..., {value!r}) — неизвестный ключ")
    assert not offenders, "Неизвестный feature_key (не попадёт в карту → default paid, но это опечатка):\n" + "\n".join(offenders)


def test_scan_actually_found_gates():
    # Защита от «тест зелёный, потому что ничего не нашли» (регресс механики скана).
    plan_gates = [c for c in _iter_gate_calls() if c[2] == "require_plan"]
    assert len(plan_gates) >= 50, f"ожидали много require_plan-гейтов, нашли {len(plan_gates)}"
