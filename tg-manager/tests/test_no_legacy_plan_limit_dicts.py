"""Guard против «мёртвых» лимит-словарей по устаревшим именам тарифов.

После миграции на два тарифа `get_plan()` возвращает только 'free'/'paid'.
Любой per-plan словарь лимитов, ключёванный старыми именами
('starter'/'pro'/'enterprise'), — тихий баг: `d.get('paid', default)` не находит
ключ и отдаёт дефолт (напр. 0 ключевых слов или 5 авто-ответов ПЛАТНИКУ вместо
безлимита). Такие словари должны браться из единого источника
(`bot/utils/tariffs.py`), а не объявляться литералами.

Тест сканирует bot/ на dict-литералы, где ключ — устаревшее имя тарифа, а
значение — число (т.е. это именно тариф→лимит), и падает на них.
"""

from __future__ import annotations

import ast
import pathlib

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"
LEGACY_PLAN_KEYS = {"starter", "pro", "enterprise"}


def _offending_dicts():
    offenders = []
    for path in BOT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            str_keys = {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if not (str_keys & LEGACY_PLAN_KEYS):
                continue
            # значения числовые? → это тариф→лимит, а не что-то другое
            numeric_vals = any(
                isinstance(v, ast.Constant) and isinstance(v.value, (int, float))
                for v in node.values
            )
            if numeric_vals:
                offenders.append(f"{path.relative_to(BOT_DIR.parent)}:{node.lineno}")
    return offenders


def test_no_plan_limit_dict_keyed_by_legacy_names():
    offenders = _offending_dicts()
    assert not offenders, (
        "Лимит-словарь по устаревшим именам тарифа (get_plan даёт free/paid → "
        "платник получит дефолт). Бери из bot/utils/tariffs.py:\n" + "\n".join(offenders)
    )
