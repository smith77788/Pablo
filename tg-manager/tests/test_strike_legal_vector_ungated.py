"""Регрессия: юридический вектор Strike был заблокирован отсутствием TG-аккаунтов.

Пользователь: 100+ страйков, ноль удалённых целей. Один из корней: массовые in-app
жалобы — слабый вектор, а реально удаляющий канал ЮРИДИЧЕСКИЙ вектор (письма
abuse@/dmca@ Telegram, CSAM→NCMEC) TG-аккаунтов НЕ требует — только SMTP. Но
_exec_strike имел ТРИ ранних `return failed` (нет аккаунтов / все в cooldown / все на
прогреве), которые глушили операцию ДО юр-вектора → единственный работающий вектор
не запускался, если флот занят/пуст/в карантине.

Фикс: пустой viable больше не фейлит операцию. Если есть хотя бы один SMTP-ящик —
уходим в legal-only (staggered_strike с пустым списком аккаунтов шлёт письма). Если
нет ни аккаунтов, ни SMTP — честный отказ с указанием подключить SMTP, а не тихое
«готово, 0».

Тесты падают без фикса: раньше пустой флот = мгновенный failed, staggered_strike не
вызывался.
"""
from __future__ import annotations

import inspect

import pytest

from services import op_worker
from tests.test_executors import FakePool


def test_exec_strike_has_no_early_fail_on_empty_fleet():
    """Ранние `return failed` по пустому флоту убраны — иначе юр-вектор не запустить."""
    src = inspect.getsource(op_worker._exec_strike)
    assert "Strike: все аккаунты в cooldown или неактивны" not in src, (
        "ранний отказ по cooldown глушил юр-вектор — должен быть убран"
    )
    assert "Strike: все аккаунты на прогреве или в cooldown" not in src, (
        "ранний отказ по прогреву глушил юр-вектор — должен быть убран"
    )
    # legal-only ветка присутствует и проверяет SMTP
    assert "strike_email_accounts" in src and "legal-only" in src.lower(), (
        "нужна legal-only ветка: без аккаунтов, но с SMTP — шлём письма"
    )


async def _no_accounts(*a, **k):
    return []


async def _passthrough_quar(pool, op_id, viable):
    return (viable, 0)


@pytest.mark.asyncio
async def test_no_accounts_no_smtp_honest_fail(monkeypatch):
    """Ни аккаунтов, ни SMTP → честный отказ с подсказкой про SMTP, не тихий ноль."""
    monkeypatch.setattr("services.resource_selector.select_all_active", _no_accounts)
    monkeypatch.setattr(op_worker, "_filter_quarantined_accounts", _passthrough_quar)
    pool = FakePool(fetch=[], fetchrow=None, fetchval=0)  # 0 активных SMTP
    res = await op_worker._exec_strike(
        pool, None, 1, 10, {"target": "@bad", "reason": "spam", "force": True})
    assert res["status"] == "failed"
    assert "SMTP" in res["summary"], "отказ должен вести к подключению SMTP"


@pytest.mark.asyncio
async def test_no_accounts_but_smtp_runs_legal_only(monkeypatch):
    """Аккаунтов нет, но SMTP есть → операция НЕ фейлится, а идёт в staggered_strike
    (legal-only: письма аккаунтов не требуют)."""
    monkeypatch.setattr("services.resource_selector.select_all_active", _no_accounts)
    monkeypatch.setattr(op_worker, "_filter_quarantined_accounts", _passthrough_quar)

    captured = {}

    async def _fake_staggered(plan, **kwargs):
        captured["accounts"] = list(plan.accounts)
        captured["owner_id"] = plan.owner_id
        return []  # никаких целей-результатов — важно, что дошли до запуска

    # staggered_strike импортируется внутри _exec_strike из strike_engine — патчим там
    monkeypatch.setattr("services.strike_engine.staggered_strike", _fake_staggered)

    pool = FakePool(fetch=[], fetchrow={"mode": "normal"}, fetchval=2)  # 2 SMTP
    res = await op_worker._exec_strike(
        pool, None, 2, 10, {"target": "@bad", "reason": "spam", "force": True})

    assert "accounts" in captured, "staggered_strike должен быть вызван (legal-only)"
    assert captured["accounts"] == [], "legal-only идёт с пустым списком аккаунтов"
    assert res["status"] != "failed", "с настроенным SMTP операция не должна фейлиться"
