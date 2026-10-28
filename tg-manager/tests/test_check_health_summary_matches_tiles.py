"""Итог «Проверить все» обязан сходиться с плитками экрана «Аккаунты».

Жалоба со скриншотов: плитки «Все 52 · Активных 49 · Спамблок 2 · Мёртвые 1»,
а плашка итога проверки — «Проверено 52 · активен: 46 · сессия истекла: 1».
Пять аккаунтов пропали из виду. Причины:
  1. итог писался строкой на статус по алфавиту кода, а плашка показывает
     первые три строки — спам-блок и «ошибка проверки» уходили за обрез молча;
  2. ошибка связи статус в базе не меняет (так и надо), поэтому в плитках такой
     аккаунт оставался «Активным» — и итог об этом не говорил;
  3. при auth-ошибке аккаунт выключался, но вердикт в acc_status не писался —
     он выпадал из «Активных» и не попадал в «Мёртвые».
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import op_worker


class _FakePool:
    def __init__(self, accounts):
        self._accounts = accounts
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *a):
        return self._accounts

    async def execute(self, query, *a):
        self.executed.append((query, a))
        return "UPDATE 0"

    async def fetchval(self, query, *a):
        return 0


def _acc(i):
    return {"id": i, "session_str": "s" * 20, "first_name": f"A{i}",
            "phone": "+1", "username": "u", "proxy_url": None}


def _check(results):
    pool = _FakePool([_acc(i) for i in range(1, len(results) + 1)])
    with patch("services.account_manager.check_account_status_full",
               AsyncMock(side_effect=results)), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = asyncio.run(op_worker._exec_check_accounts_health(
            pool, None, 5, 99, {"check_spambot": True}))
    return res, pool


def test_whole_breakdown_fits_in_the_popup():
    res, _ = _check([
        {"status": "active", "reason": "ok"},
        {"status": "active", "reason": "ok"},
        {"status": "unknown", "reason": "сеть"},
        {"status": "spamblock", "spamblock_kind": "perm", "reason": "x"},
        {"status": "session_expired", "reason": "x", "auth_error": True},
    ])
    lines = res["summary"].split("\n")
    # Плашка показывает три строки — каждый статус обязан быть в них.
    head = "\n".join(lines[:3])
    for label in ("активен: 2", "спам-блок: 1", "сессия отозвана: 1",
                  "ошибка проверки: 1"):
        assert label in head, res["summary"]
    # Порядок по важности: активные первыми, ошибки проверки последними.
    breakdown = lines[1]
    assert breakdown.index("активен") < breakdown.index("спам-блок") \
        < breakdown.index("сессия") < breakdown.index("ошибка проверки"), breakdown
    # Сумма раскладки равна числу проверенных.
    assert sum(res["status_counts"].values()) == res["checked"] == 5


def test_unconfirmed_verdicts_are_named_in_summary():
    res, _ = _check([
        {"status": "active", "reason": "ok"},
        {"status": "unknown", "reason": "сеть"},
        {"status": "unknown", "reason": "сеть"},
    ])
    assert res["unchanged"] == 2
    assert "Без подтверждения: 2" in res["summary"]
    assert "статус не менялся" in res["summary"]


def test_auth_error_writes_verdict_not_only_is_active():
    _, pool = _check([
        {"status": "session_expired", "reason": "отозван", "auth_error": True},
    ])
    deact = [(q, a) for q, a in pool.executed if "is_active=FALSE" in q]
    assert deact, pool.executed
    q, args = deact[0]
    assert "acc_status=$2" in q
    assert args[0] == 1 and args[1] == "session_expired"


def test_popup_marks_truncated_summary():
    src = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(
        encoding="utf-8")
    start = src.index("function showOpComplete(op)")
    body = src[start:start + 3000]
    assert "в деталях" in body, "обрез итога в плашке должен быть помечен"
