"""Человеку не показываются тарифы, которых нет.

РАЗРЫВ. Тарифов два: бесплатный и платный (bot/utils/tariffs.py), а
Starter/Pro/Enterprise остались только псевдонимами платного. Но реферальная
программа обещала «7 дней Starter», награды «30 дней Pro / Enterprise», а
гейты писали «доступно с тарифа Starter» — человек искал в /subscription
тариф, которого там нет.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEAD = ("Starter", "Enterprise", "MAX / ")
# Админский выбор, какой псевдоним записать в подписку, — не текст для клиента.
_ALLOWED = {("bot/handlers/admin_users.py", "⭐ Starter"), ("bot/handlers/admin_users.py", "👑 Enterprise")}


def _literals(path: pathlib.Path):
    tree = ast.parse(path.read_text("utf-8"))
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                doc_ids.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            yield node.value


def test_user_facing_texts_do_not_name_removed_tariffs():
    files = sorted((ROOT / "bot" / "handlers").glob("*.py")) + [ROOT / "database" / "db.py"]
    bad = []
    for path in files:
        rel = str(path.relative_to(ROOT))
        for text in _literals(path):
            if any(w in text for w in _DEAD) and (rel, text) not in _ALLOWED:
                bad.append(f"{rel}: {text[:70]!r}")
    assert not bad, "тексты называют несуществующий тариф:\n" + "\n".join(bad)


def test_referral_reward_label_is_the_real_tariff():
    from bot.handlers import referral

    for _lvl, _m, _t, plan, _d, _label in referral._TIERS:
        assert referral._PLAN_LABEL[plan] == "подписки"
