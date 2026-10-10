"""Anti-detection: Strike (самая баноопасная операция) уважает риск-пульс.

`_exec_strike` фильтровал аккаунты через preflight (cooldown/flood) и warmup-guard,
но НЕ через `is_account_quarantined` (недавнее СЕРЬЁЗНОЕ ограничение из
restriction_events). Жалоба на цель через уже флагнутый аккаунт = быстрый хард-бан
(anti-detection слой — дороже обычной фичи). Гейт-парность с mass_report, где такой
фильтр уже стоит. Общий помощник `_filter_quarantined_accounts` (fail-open).
"""
from __future__ import annotations

import inspect

from services import op_worker


def test_exec_strike_uses_quarantine_gate():
    src = inspect.getsource(op_worker._exec_strike)
    assert "_filter_quarantined_accounts" in src, (
        "_exec_strike обязан фильтровать аккаунты под риск-пульсом — "
        "жалоба с флагнутого аккаунта = быстрый бан"
    )
    assert "риск-пульс" in src, "должен честно логировать пропуск карантинных аккаунтов"


def test_exec_strike_gate_after_preflight():
    # Гейт должен идти ПОСЛЕ preflight_accounts (сначала cooldown/flood, потом
    # риск-пульс поверх выживших) и ДО plan_waves (в бой уходят только чистые).
    src = inspect.getsource(op_worker._exec_strike)
    i_pre = src.find("preflight_accounts")
    i_gate = src.find("_filter_quarantined_accounts")
    i_waves = src.rfind("plan_waves(")
    assert i_pre != -1 and i_gate != -1 and i_waves != -1
    assert i_pre < i_gate < i_waves, "гейт риск-пульса — между preflight и plan_waves"
