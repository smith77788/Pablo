"""Content Clone (#7) — регрессия: гейт карантина перед клонированием через аккаунт.

_exec_content_clone выбирал acc = accounts[0] только по is_active, без проверки
единого пульса здоровья → клонирование могло идти через флагнутый (флуд/
ограничение) аккаунт = быстрый бан. Добавлен общий _filter_quarantined_accounts
перед выбором аккаунта (как в ai_comment/boost-исполнителях).
"""
from __future__ import annotations

import inspect

from services import op_worker


def test_clone_filters_quarantine_before_account_pick():
    src = inspect.getsource(op_worker._exec_content_clone)
    filt = src.index("_filter_quarantined_accounts")
    pick = src.index("acc = dict(accounts[0])")
    clone = src.index("clone_to_channel(")
    assert filt < pick < clone, "quarantine-гейт должен быть до выбора аккаунта и клонирования"
