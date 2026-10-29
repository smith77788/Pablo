"""Храповик: аккаунт и план прогрева пишутся каждый одной дверью.

До 29.10.2026 аккаунт записывался в базу пятью путями (общая запись, строка
сессии, авторегистрация и два UPDATE сессии в боте), а план прогрева — шестью.
Пути расходились в самом важном: дедупе по личности, пустом номере, снятии
залипших блокировок, проверке, что аккаунт жив. Теперь:

  • `tg_accounts` создаётся только в `database/db.py::upsert_tg_account`;
  • `account_warmup_plans` создаётся только в `services/account_warmer.py`
    (`create_warmup_plan` и `create_warmup_plans_bulk`).

Новый INSERT в другом файле — это новый расходящийся путь. Нужен вход —
зовите общую функцию.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN = ("services", "bot", "database", "mini_app")
_ALLOWED = {
    "tg_accounts": {"database/db.py"},
    "account_warmup_plans": {"services/account_warmer.py"},
}


def _writers(table: str) -> set[str]:
    pat = re.compile(rf"INSERT\s+INTO\s+{table}\b", re.IGNORECASE)
    found = set()
    for top in _SCAN:
        for f in (_ROOT / top).rglob("*.py"):
            if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
                found.add(f.relative_to(_ROOT).as_posix())
    return found


def test_accounts_are_created_in_one_place():
    extra = _writers("tg_accounts") - _ALLOWED["tg_accounts"]
    assert not extra, (
        f"аккаунт пишется мимо db.upsert_tg_account: {sorted(extra)}")


def test_warmup_plans_are_created_in_one_place():
    extra = _writers("account_warmup_plans") - _ALLOWED["account_warmup_plans"]
    assert not extra, (
        f"план прогрева пишется мимо account_warmer.create_warmup_plan: {sorted(extra)}")


def test_account_rows_have_a_single_insert_in_db():
    src = (_ROOT / "database" / "db.py").read_text(encoding="utf-8")
    n = len(re.findall(r"INSERT\s+INTO\s+tg_accounts\b", src))
    assert n == 1, f"в db.py {n} INSERT INTO tg_accounts — должна остаться одна дверь"
