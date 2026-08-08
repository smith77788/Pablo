"""Регресс «несуществующая колонка» для infra_copilot (JOIN-SELECT).

Баг (2026-07-12): _analyze_account_patterns селектил `h7.avg_score` из
account_health_history, но такой колонки нет (реальные: health_score/load_score/
trust_score/…). Запрос падал UndefinedColumnError, проглатывался внешним try →
инсайт «деградация доверия аккаунтов» НИКОГДА не появлялся (тихо мёртвая ветка
копайлота). Исправлено на trust_score (та же шкала 0–1, что и порог -0.1).

Тест строит колонки account_health_history из схемы и проверяет, что все
`h7.<col>` в infra_copilot существуют.
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _ahh_columns() -> set[str]:
    cols: set[str] = set()
    for f in sorted(glob.glob(os.path.join(_ROOT, "schema*.sql"))):
        s = open(f, encoding="utf-8", errors="replace").read()
        for m in re.finditer(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+account_health_history\s*\((.*?)\n\)\s*;",
            s, re.DOTALL | re.I,
        ):
            for line in m.group(1).split("\n"):
                line = line.strip().rstrip(",")
                mm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+', line, re.I)
                if mm and mm.group(1).upper() not in ("PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"):
                    cols.add(mm.group(1).lower())
    return cols


def test_infra_copilot_h7_columns_exist():
    cols = _ahh_columns()
    assert "trust_score" in cols and "health_score" in cols, "схема не распозналась"
    src = open(os.path.join(_ROOT, "services", "infra_copilot.py"), encoding="utf-8").read()
    refs = set(re.findall(r"\bh7\.([a-z_][a-z0-9_]*)", src))
    missing = refs - cols
    assert not missing, (
        f"infra_copilot ссылается на h7(account_health_history).колонки, которых нет: {sorted(missing)}"
    )


def test_no_avg_score_reference():
    src = open(os.path.join(_ROOT, "services", "infra_copilot.py"), encoding="utf-8").read()
    assert "avg_score" not in src, "avg_score (несуществующая колонка) не должна упоминаться"
