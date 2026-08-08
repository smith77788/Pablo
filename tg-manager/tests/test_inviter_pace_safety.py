"""Регресс: бот-инвайтер прокидывает ban-safety параметры (pace + лимит).

Инвайт — самая баноопасная операция. mini-app давал pace/max_invites/
per_account_limit, а бот ставил mass_invite ТОЛЬКО с batch_size → бот-инвайты шли
неограниченно на нормальном темпе. Теперь бот спрашивает темп и ставит потолок
на аккаунт. Проверяем всю цепочку: кнопки → хендлер → params → исполнитель.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_pace_buttons_offered():
    h = _read("bot/handlers/mass_inviter.py")
    assert 'InviterCb(action="confirm", item="slow")' in h
    assert 'InviterCb(action="confirm", item="normal")' in h
    assert 'InviterCb(action="confirm", item="fast")' in h


def test_confirm_reads_pace_from_callback():
    h = _read("bot/handlers/mass_inviter.py")
    # хендлер принимает callback_data и читает item как pace
    assert "callback_data: InviterCb" in h
    assert 'callback_data.item if callback_data.item in ("slow", "normal", "fast")' in h


def test_params_include_pace_and_per_account_limit():
    h = _read("bot/handlers/mass_inviter.py")
    assert '"pace": pace' in h
    assert '"per_account_limit": 50' in h


def test_executor_honors_pace_and_per_account_limit():
    ow = _read("services/op_worker.py")
    seg = ow[ow.index("async def _exec_mass_invite"):ow.index("async def _exec_bulk_set_profile")]
    # темп реально влияет на паузу между батчами
    assert 'params.get("pace")' in seg
    assert '_batch_delay' in seg and '_pace_mult' in seg
    # лимит на аккаунт реально обрезает работу аккаунта
    assert 'params.get("per_account_limit")' in seg
    assert '_per_acc_limit' in seg


def test_pace_multiplier_values_sane():
    ow = _read("services/op_worker.py")
    # slow медленнее normal, fast быстрее — иначе выбор темпа бессмыслен
    seg = ow[ow.index("async def _exec_mass_invite"):ow.index("async def _exec_bulk_set_profile")]
    assert '"slow": 2.5' in seg and '"normal": 1.0' in seg and '"fast": 0.5' in seg
