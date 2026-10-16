"""Инвайт клеймит аккаунты — защита от AUTH_KEY_DUPLICATED (сессия с двух IP).

Скриншот (оп #112): все аккаунты упали на connect с «authorization key used under
two different IP addresses simultaneously». Одна из контролируемых нами причин:
инвайт был ЕДИНСТВЕННЫМ массовым исполнителем, который НЕ клеймил аккаунты
(mark/claim), в отличие от strike/warmup/publish. Без клейма прогрев или другая
операция мог подключить ту же сессию параллельно = два IP одновременно = бан ключа.

Фикс: _exec_mass_invite берёт аккаунты через _claim_available_accounts(op_id, …) —
атомарно только свободные, помечает занятыми; освобождение автоматически в finally
_run_op_task (release_operation_accounts по op_id), утечки нет.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def test_invite_claims_accounts_atomically():
    body = _exec_body()
    assert "_claim_available_accounts(op_id" in body, (
        "инвайт обязан клеймить аккаунты (как strike/warmup/publish) — иначе "
        "параллельное использование сессии = AUTH_KEY_DUPLICATED"
    )


def test_invite_handles_all_accounts_busy():
    body = _exec_body()
    # после клейма может не остаться свободных — честный ответ, а не «работали 0 из 0»
    assert "заняты другими операциями" in body, (
        "если все аккаунты заняты — нужно явно сказать, а не молча выдать нули"
    )


def test_release_is_automatic_by_op_id():
    # _run_op_task освобождает claim по op_id в finally — значит клейм по op_id
    # не требует ручного release в инвайте (и не течёт при ошибке).
    m = re.search(r"finally:.*?release_operation_accounts\(op_id\)", WORKER, re.DOTALL)
    assert m, "claim по op_id должен авто-освобождаться в finally _run_op_task"


def test_claim_binds_to_operation_lock():
    # _claim_available_accounts связывает claim с _operation_account_locks[op_id],
    # что и позволяет release_operation_accounts(op_id) всё вернуть.
    src = re.search(r"async def _claim_available_accounts\(.*?(?=\nasync def |\ndef )",
                    WORKER, re.DOTALL).group(0)
    assert "_operation_account_locks.setdefault(op_id" in src
    assert "not in _accounts_in_use" in src, "клеймим только свободные (атомарно)"
