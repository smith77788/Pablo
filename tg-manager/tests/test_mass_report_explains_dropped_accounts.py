"""Массовая жалоба честно объясняет, почему из выбранных пошёл не весь срез.

Жалоба владельца: «массовые жалобы берут один аккаунт вместо заданного
количества». Причина — защитные гейты (кулдаун после FloodWait, спамблок/бан,
риск-пульс, занятость) МОЛЧА выкидывают аккаунты из явного выбора. Теперь
исход раскладывает остаток по причинам: владелец видит не загадочную «1», а
«5 на кулдауне, 3 спамблок, 1 занят».
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *a):
        # Диагностика спрашивает статусы выбранных аккаунтов.
        if "from tg_accounts" in query.lower():
            return self._rows
        return []


def _row(aid, *, is_active=True, has_sess=True, acc_status="active", on_cooldown=False):
    return {"id": aid, "is_active": is_active, "has_sess": has_sess,
            "acc_status": acc_status, "on_cooldown": on_cooldown}


def _run(coro):
    return asyncio.run(coro)


def test_breakdown_names_each_reason():
    # Выбрано 6, в работу пошёл 1 (id=1). Остальные пятеро — по разным причинам.
    rows = [
        _row(2, on_cooldown=True),                 # кулдаун
        _row(3, on_cooldown=True),                 # кулдаун
        _row(4, acc_status="spamblock"),           # спамблок
        _row(5, is_active=False),                  # без сессии/неактивен
        _row(6),                                   # активен, не в кулдауне → занят/карантин
    ]
    with patch("services.infra_memory.quarantined_accounts",
               AsyncMock(return_value={6})):       # id=6 в риск-пульсе
        frag, counts = _run(op_worker._diagnose_unused_accounts(
            _FakePool(rows), 99, [1, 2, 3, 4, 5, 6], {1}))
    assert counts["cooldown"] == 2, counts
    assert counts["spamblock"] == 1, counts
    assert counts["no_session"] == 1, counts
    assert counts["quarantine"] == 1, counts
    assert counts["busy"] == 0, counts
    # В тексте названы кулдаун, спамблок, риск-пульс и «без сессии».
    assert "кулдаун" in frag.lower()
    assert "спамблок" in frag.lower()
    assert "риск-пульс" in frag.lower()
    assert "Не пошли в работу 5" in frag


def test_busy_when_healthy_but_not_used():
    # Аккаунт прошёл все статусные гейты (активен, не в кулдауне, не в карантине),
    # но в used его нет → он был занят другой операцией на момент захвата.
    rows = [_row(2)]
    with patch("services.infra_memory.quarantined_accounts",
               AsyncMock(return_value=set())):
        frag, counts = _run(op_worker._diagnose_unused_accounts(
            _FakePool(rows), 99, [1, 2], {1}))
    assert counts["busy"] == 1, counts
    assert "заняты" in frag.lower()


def test_no_fragment_when_all_used():
    frag, counts = _run(op_worker._diagnose_unused_accounts(
        _FakePool([]), 99, [1, 2, 3], {1, 2, 3}))
    assert frag == "" and counts == {}


def test_mass_report_summary_wires_the_breakdown():
    """Сводка исполнителя реально зовёт диагностику и показывает причины."""
    import ast
    import inspect
    src = inspect.getsource(op_worker._exec_mass_report)
    assert "_diagnose_unused_accounts(" in src, (
        "mass_report обязан раскладывать, почему из выбранного пошло меньше")
    # вызвано именно на явном выборе владельца (account_ids) против реально
    # отработавших (used_ids)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("_diagnose_unused_accounts")]
    assert calls, "вызов диагностики не найден"
    args = ast.unparse(calls[0])
    assert "account_ids" in args and "used_ids" in args
