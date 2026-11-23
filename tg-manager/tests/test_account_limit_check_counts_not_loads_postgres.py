"""Меню аккаунтов не тащит из базы весь флот ради одного числа.

* `get_tg_accounts` вёз зашифрованную сессию ДВАЖДЫ (`session_str` и её
  псевдоним `session_string`) в каждой строке — на флоте в тысячи аккаунтов
  это мегабайты на каждое открытие меню. Читатели берут
  `acc.get("session_str") or acc.get("session_string")`, так что псевдоним
  ничего не добавлял.
* Лимит тарифа в четырёх местах (добавить, QR-вход, импорт, завершение
  импорта) проверялся как `len(await get_tg_accounts(...))` — весь флот с
  сессиями ради COUNT.

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_733
ROOT = Path(__file__).resolve().parent.parent
LIMIT_CHECKS = ("cb_add_account", "cb_qr_login", "cb_import_menu", "_finalize_import")


def _go(body):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await body(p)
        finally:
            await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await p.close()

    asyncio.run(_main())


def test_fleet_row_carries_the_session_once_and_count_is_exact():
    from database import db

    async def body(pool):
        await pool.execute(
            "INSERT INTO tg_accounts (owner_id, phone, session_str, is_active) "
            "SELECT $1, '+7733' || g, 'enc-' || g, TRUE FROM generate_series(1, 7) g",
            OWNER)
        rows = await db.get_tg_accounts(pool, OWNER)
        assert len(rows) == 7 == await db.count_tg_accounts(pool, OWNER)
        assert "session_string" not in rows[0].keys()
        r = rows[0]
        # Тот же приём, которым сессию читают обработчики меню.
        assert (r.get("session_str") or r.get("session_string")).startswith("enc-")
        assert r["has_session"] is True
        assert await db.count_tg_accounts(pool, OWNER + 1) == 0

    _go(body)


def test_tariff_limit_checks_count_instead_of_loading_the_fleet():
    src = (ROOT / "bot/handlers/accounts.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) and fn.name in LIMIT_CHECKS:
            body = ast.get_source_segment(src, fn)
            seen.add(fn.name)
            assert "count_tg_accounts(" in body, f"{fn.name}: лимит не считается COUNT"
            assert "get_tg_accounts(" not in body, (
                f"{fn.name}: ради проверки лимита грузится весь флот")
    assert seen == set(LIMIT_CHECKS), f"проверка ослепла: нашлись только {seen}"
