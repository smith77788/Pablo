"""Регресс «несуществующая колонка» для crm_deals в auto_responder.

Баг (2026-07-12): экшен авто-ответа `create_deal` делал
`INSERT INTO crm_deals(bot_id, user_id, title, status, created_at)`, но реальная
схема crm_deals (schema_v90) — owner-scoped: (owner_id, title, contact, stage,
value, notes, created_at, updated_at). bot_id/user_id/status НЕ существуют →
INSERT падал UndefinedColumnError (проглатывался внешним try) → сделка из лида
никогда не создавалась. Исправлено: резолв владельца из managed_bots.added_by +
INSERT по реальным колонкам (contact=лид, stage='new').

Тест строит колонки crm_deals из схемы и проверяет, что каждый
`INSERT INTO crm_deals(...)` в auto_responder использует только существующие
колонки. Ловит будущие рассинхроны код↔схема того же класса.
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _crm_deals_columns() -> set[str]:
    cols: set[str] = set()
    for f in sorted(glob.glob(os.path.join(_ROOT, "schema*.sql"))):
        s = open(f, encoding="utf-8", errors="replace").read()
        for m in re.finditer(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+crm_deals\s*\((.*?)\n\)\s*;",
            s, re.DOTALL | re.I,
        ):
            for line in m.group(1).split("\n"):
                line = line.strip().rstrip(",")
                mm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+', line, re.I)
                if mm and mm.group(1).upper() not in (
                    "PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK",
                ):
                    cols.add(mm.group(1).lower())
        for m in re.finditer(
            r'ALTER TABLE\s+crm_deals\s+(.*?);', s, re.DOTALL | re.I,
        ):
            for cm in re.finditer(
                r'ADD COLUMN(?:\s+IF NOT EXISTS)?\s+"?([a-z_][a-z0-9_]*)"?', m.group(1), re.I
            ):
                cols.add(cm.group(1).lower())
    return cols


def test_auto_responder_crm_deals_inserts_use_existing_columns():
    cols = _crm_deals_columns()
    assert "owner_id" in cols and "stage" in cols, "схема crm_deals не распозналась"
    src = open(os.path.join(_ROOT, "services", "auto_responder.py"), encoding="utf-8").read()
    found = 0
    for m in re.finditer(r"INSERT INTO\s+crm_deals\s*\(([^)]*)\)", src, re.I):
        found += 1
        insert_cols = [c.strip().strip('"').lower() for c in m.group(1).split(",") if c.strip()]
        missing = [c for c in insert_cols if c not in cols]
        assert not missing, (
            f"INSERT INTO crm_deals ссылается на несуществующие колонки {missing}; "
            f"есть: {sorted(cols)}"
        )
    assert found >= 2, "ожидались оба INSERT INTO crm_deals (create_deal в 2 местах)"
