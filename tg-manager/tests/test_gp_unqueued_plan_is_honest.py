"""Гео-план, чья операция не встала в очередь, не остаётся зомби «Ожидает».

Жалоба владельца: «гео-сеть не начинает создавать» — план висит «Ожидает 0/30»
и не стартует, а кнопка «Повтор» его не оживляет.

Причина: _submit_plan создаёт план (status='queued') и цели (status='pending')
ДО постановки операции. Если submit падает (тариф/предохранитель/сбой), op_id
становится None, но план остаётся 'queued' навсегда — операции за ним нет.
Хуже того: кнопка «Повтор» зовёт reset_failed_targets, который сбрасывает
ТОЛЬКО цели со статусом 'failed'. У зомби-плана все цели 'pending' → «нет
повторяемых ошибок» → оживить план нечем.

Фикс: при провале постановки и план, и его цели переводятся в 'failed' с
причиной. Статус становится честным, а reset_failed_targets находит цели и
повтор начинает работать.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    """Пул, который запоминает execute-запросы и их аргументы."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        return 0


def _patch(monkeypatch, *, submit_raises: bool):
    from bot.handlers import global_presence as gp

    created = {}

    async def _create_plan(pool, **kw):
        return 4242

    async def _create_targets(pool, plan_id, targets):
        created["targets"] = len(targets)
        return len(targets)

    async def _submit(pool, owner_id, op_type, params, **kw):
        if submit_raises:
            raise gp.operation_bus.PlanRequiredError(op_type, "enterprise")
        return 999

    async def _link(pool, plan_id, op_id):
        created["linked"] = (plan_id, op_id)

    monkeypatch.setattr(gp.db, "create_global_presence_plan", _create_plan)
    monkeypatch.setattr(gp.db, "create_global_presence_targets", _create_targets)
    monkeypatch.setattr(gp.operation_bus, "submit", _submit)
    monkeypatch.setattr(gp.db, "link_plan_to_operation", _link)
    return gp


SD = {"name_pattern": "Доставка {{CITY_NAME}}", "username_pattern": None,
      "geo_preset": "ru_top", "geo_list": [{"city": "Москва"}],
      "selected_acc_ids": [1], "template_id": None}
TARGETS = [{"asset_type": "channel", "planned_name": "Доставка Москва"}]


@pytest.mark.asyncio
async def test_unqueued_plan_marked_failed_and_retryable(monkeypatch):
    """Главный регресс: submit упал → план и цели становятся 'failed'."""
    gp = _patch(monkeypatch, submit_raises=True)
    pool = _Pool()

    plan_id, op_id = await gp._submit_plan(
        pool, 555, SD, TARGETS, "global_presence_full_package", "full_package")

    assert plan_id == 4242
    assert op_id is None, "операция не встала — op_id обязан быть None"

    joined = " || ".join(q for q, _ in pool.calls)
    assert "UPDATE global_presence_plans SET status='failed'" in joined, (
        "план остался 'queued' — зомби «Ожидает» навсегда"
    )
    # Цели переводятся в 'failed' И остаются retryable — иначе reset_failed_targets
    # их не подхватит и «Повтор» снова скажет «нет повторяемых ошибок».
    tgt_update = [a for q, a in pool.calls
                  if "global_presence_targets SET status='failed'" in q]
    assert tgt_update, "цели не помечены 'failed' — кнопка «Повтор» мертва"
    assert any("retryable=TRUE" in q for q, _ in pool.calls), (
        "цели не оставлены повторяемыми — повтор невозможен"
    )
    # Причина сохранена, чтобы меню показало её вместо «непонятной информации».
    assert any(isinstance(a, tuple) and len(a) >= 2
               and isinstance(a[1], str) and "не поставлена в очередь" in a[1]
               for _, a in pool.calls), "причина отказа не записана в цели"


@pytest.mark.asyncio
async def test_successful_submit_leaves_plan_alone(monkeypatch):
    """Контроль: успешная постановка НЕ помечает план failed."""
    gp = _patch(monkeypatch, submit_raises=False)
    pool = _Pool()

    plan_id, op_id = await gp._submit_plan(
        pool, 555, SD, TARGETS, "global_presence_channel", "channel")

    assert op_id == 999
    joined = " || ".join(q for q, _ in pool.calls)
    assert "status='failed'" not in joined, (
        "успешный план ошибочно помечен как провалившийся"
    )
