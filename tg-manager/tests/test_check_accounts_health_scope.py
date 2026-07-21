"""«Проверить все» аккаунты: скоуп и админский межтенантный случай.

Со скриншота: админ запустил «Проверить все», выбрало 6 (из 25) и упало 0/6.
Две связанные причины (класс admin cross-tenant vs owner-scoped):
  1. submit accounts_check для админа фильтровал is_active=TRUE → 6 из 25 (и
     противоречил обещанию кнопки «ошибочно отключённые будут восстановлены»);
  2. исполнитель _exec_check_accounts_health фильтровал WHERE a.owner_id=$1 —
     но админ владеет 0 из этих аккаунтов → owner=админ AND id IN(чужие) = 0 строк
     → падение 0/6.
Фикс: submit(admin) берёт ВСЕ аккаунты; исполнитель по явному (уже авторизованному
на сервере) списку id фильтрует ТОЛЬКО по id, без owner_id.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import op_worker


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class CapturePool:
    def __init__(self):
        self.fetch_sql = None
        self.fetch_args = None

    async def fetch(self, sql, *args):
        self.fetch_sql = sql
        self.fetch_args = args
        return []  # нам важен сам запрос; пусто → executor вернёт failed

    async def execute(self, *a, **k):
        return "UPDATE 0"

    async def fetchval(self, *a, **k):
        return 0


def test_executor_uses_id_only_filter_for_explicit_ids():
    pool = CapturePool()
    # owner_id=999 (админ, владеет 0), account_ids принадлежат другим владельцам
    _run(op_worker._exec_check_accounts_health(
        pool, None, 1, 999, {"account_ids": [11, 22, 33]}))
    assert pool.fetch_sql is not None
    assert "a.id = ANY($1" in pool.fetch_sql, pool.fetch_sql
    # НЕ должно быть owner_id в фильтре по явному списку (иначе чужие → 0 строк)
    assert "owner_id" not in pool.fetch_sql, pool.fetch_sql
    assert pool.fetch_args == ([11, 22, 33],), pool.fetch_args


def test_executor_falls_back_to_owner_scope_without_ids():
    pool = CapturePool()
    _run(op_worker._exec_check_accounts_health(pool, None, 1, 42, {}))
    assert "a.owner_id=$1" in pool.fetch_sql, pool.fetch_sql
    assert pool.fetch_args == (42,), pool.fetch_args


def test_submit_admin_checks_all_accounts_not_only_active():
    src = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    m = re.search(r"async def accounts_check\(request.*?\n(.*?)\n    def _build_profile_params",
                  src, re.DOTALL)
    assert m, "accounts_check не найден"
    body = m.group(1)
    # админская ветка выбирает ВСЕ аккаунты (без is_active-фильтра)
    assert 'rows = await _safe_fetch(pool, "SELECT id FROM tg_accounts")' in body, body[:600]
    assert "is_active=TRUE" not in body, "«Проверить все» не должно фильтровать по is_active"
