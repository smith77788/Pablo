"""Регресс: mass_invite ставится через operation_bus.submit(), не сырым INSERT.

Первопричина: services/operation_bus.py прямо запрещает прямой INSERT INTO
operation_queue в handler'ах («Заменяет прямые INSERT INTO operation_queue в
20+ handler-файлах... Прямые INSERT INTO operation_queue в новых handler'ах —
запрещены»), но и bot/handlers/mass_inviter.py, и mini_app_api.mass_inviter_submit
делали именно это — для mass_invite, САМОЙ рискованной операции продукта
(min_plan='pro', max_retries=2 в OP_REGISTRY). Из-за этого молча обходились:
  • план-гейт (free-юзер мог поставить платную операцию — except PermissionError
    в mini_app_api.py уже был написан в расчёте на PlanRequiredError, но
    submit() никогда не вызывался, ветка была мертва);
  • предохранитель Ban Weather (Фаза 3) — единственная операция в продукте,
    для которой шторм банов НЕ останавливал новые прогоны;
  • дедуп повторной постановки;
  • max_retries из OP_REGISTRY (сырой INSERT ставил дефолт схемы, не 2).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_op_registry_declares_mass_invite_risk_contract():
    src = _read("services/operation_bus.py")
    seg = src[src.index('"mass_invite": {'):]
    seg = seg[:seg.index("},")]
    assert '"min_plan": "pro"' in seg
    assert '"max_retries": 2' in seg


def test_bot_handler_uses_submit_not_raw_insert():
    src = _read("bot/handlers/mass_inviter.py")
    assert "operation_bus.submit(" in src, "бот не вызывает operation_bus.submit"
    # старый сырой INSERT для mass_invite убран из кода (не в докстринге/комментарии)
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label)" not in code
    # оба типа отказа обработаны отдельно (paywall vs предохранитель)
    assert "except PlanRequiredError" in src
    assert "except ImmunityBlockedError" in src
    assert "subscription_locked_markup" in src


def test_mini_app_handler_uses_submit_not_raw_insert():
    src = _read("services/mini_app_api.py")
    seg = src[src.index("async def mass_inviter_submit"):]
    seg = seg[:seg.index("\n    # ── Stars Hub")]
    assert "_obus.submit(" in seg, "мини-апп не вызывает operation_bus.submit"
    code_lines = [ln for ln in seg.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label)" not in code
    # except PermissionError теперь реально достижим (submit реально вызывается)
    assert "except PermissionError as exc:" in seg
