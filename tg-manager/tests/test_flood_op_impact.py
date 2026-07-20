"""Ядро, шаг 5: видно, что операция сделала с аккаунтами (петля «операция → след»).

Два дефекта:
  1. record_flood ПРИНИМАЛ operation_id, но НЕ писал его в account_flood_log —
     классический «параметр принят, но не доходит до эффекта». Из-за этого нельзя
     было связать FloodWait с операцией.
  2. Деталь операции не показывала влияние на аккаунты.
Плюс укрепление: кулдаун-UPDATE вынесен из общего try с лог-INSERT, чтобы сбой
записи лога не отменял кулдаун (иначе аккаунт работает во флуд → риск бана).
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

from services import flood_engine, mini_app_api


class _CapPool:
    def __init__(self):
        self.calls = []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


def test_record_flood_persists_operation_id():
    pool = _CapPool()
    asyncio.run(flood_engine.record_flood(pool, 12345, 60, "publish", operation_id=999))
    inserts = [(q, a) for q, a in pool.calls if "INSERT INTO account_flood_log" in q]
    assert inserts, "должен писать в account_flood_log"
    q, a = inserts[0]
    assert "operation_id" in q, "INSERT должен включать колонку operation_id"
    assert 999 in a, "operation_id должен попасть в аргументы INSERT"


def test_cooldown_update_independent_of_log_insert():
    """Кулдаун применяется своим execute — не в одном try с лог-INSERT."""
    src = inspect.getsource(flood_engine.record_flood)
    # UPDATE cooldown идёт ПЕРЕД INSERT в лог (критичное — первым)
    i_upd = src.index("UPDATE tg_accounts")
    i_ins = src.index("INSERT INTO account_flood_log")
    assert i_upd < i_ins, "кулдаун-UPDATE должен идти перед лог-INSERT"
    # у них разные except-обработчики (не общий try)
    assert src.count("except Exception") >= 2


def test_operation_status_returns_account_impact_scoped():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def operation_status\(.*?\n(.*?)\n    async def operation_log",
                  src, re.DOTALL)
    assert m, "operation_status не найден"
    body = m.group(1)
    assert "accounts_impact" in body
    assert "FROM account_flood_log f" in body and "f.operation_id=$1" in body
    assert "a.owner_id=$2" in body, "влияние должно скоупиться по владельцу"


def test_op_detail_renders_account_impact():
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "o.accounts_impact" in html
    assert "Влияние на аккаунты" in html
