"""Возможность: настойчивая эскалация Strike — давить, пока цель не снята.

Реальные тейкдауны идут ВО ВРЕМЕНИ: площадка/модерация реагирует не мгновенно.
«100% удаление», которое видит покупатель у сервисов, — это в т.ч. настойчивость:
давят повторно, пока цель не снята. Раньше Strike был разовым залпом. Добавлено:
при persist=True и подтверждённо живой цели (verified_down is False) движок ставит
следующий заход через ~12ч (до _MAX_STRIKE_CHAIN), останавливается при снятии цели.

Честность: verified_down None («не удалось проверить») НЕ продолжаем автоматически —
не жжём флот вслепую. Гарантий не обещаем: если исчерпан лимит — честно сообщаем.
"""
from __future__ import annotations

import inspect

import pytest

from services import op_worker


def test_schedule_continuation_uses_operation_bus_not_direct_insert():
    src = inspect.getsource(op_worker._schedule_strike_continuation)
    assert "operation_bus" in src and "submit" in src, (
        "эскалация обязана идти через шину, а не прямым INSERT"
    )
    assert "scheduled_for" in src, "следующий заход — отложенная операция"
    # свежий флот на каждый заход + это сознательный повтор
    assert 'nxt.pop("account_ids"' in src and 'nxt["force"] = True' in src


def test_exec_strike_reads_persist_and_chain():
    src = inspect.getsource(op_worker._exec_strike)
    assert 'params.get("persist")' in src
    assert 'params.get("strike_chain")' in src
    assert "_schedule_strike_continuation(" in src, "persist без вызова планировщика мёртв"


@pytest.mark.asyncio
async def test_continuation_scheduled_when_target_still_up(monkeypatch):
    """Цель подтверждённо жива + persist → ставится следующий заход, chain+1."""
    captured = {}

    async def _fake_submit(pool, owner_id, op_type, params, **kwargs):
        captured["op_type"] = op_type
        captured["params"] = params
        captured["scheduled_for"] = kwargs.get("scheduled_for")
        captured["bypass_plan_check"] = kwargs.get("bypass_plan_check")
        return 999

    import services.operation_bus as obus
    monkeypatch.setattr(obus, "submit", _fake_submit)

    op_id = await op_worker._schedule_strike_continuation(
        None, 10, {"target": "@bad", "reason": "drugs", "strike_chain": 0}, 1)
    assert op_id == 999
    assert captured["op_type"] == "strike"
    assert captured["params"]["persist"] is True
    assert captured["params"]["strike_chain"] == 1
    assert captured["params"]["force"] is True
    assert "account_ids" not in captured["params"], "флот выбирается заново каждый заход"
    assert captured["bypass_plan_check"] is True
    assert captured["scheduled_for"], "заход должен быть отложенным"


@pytest.mark.asyncio
async def test_continuation_never_raises_on_submit_failure(monkeypatch):
    """Сбой постановки эскалации не должен ронять успешный прогон — возвращает None."""
    async def _boom(*a, **k):
        raise RuntimeError("bus down")

    import services.operation_bus as obus
    monkeypatch.setattr(obus, "submit", _boom)
    res = await op_worker._schedule_strike_continuation(
        None, 10, {"target": "@bad"}, 1)
    assert res is None


def test_chain_is_capped():
    assert op_worker._MAX_STRIKE_CHAIN >= 2, "нужен хотя бы один переповтор"
    # эскалация не бесконечна — есть верхний предел заходов
    src = inspect.getsource(op_worker._exec_strike)
    assert "_MAX_STRIKE_CHAIN" in src, "цепочка заходов должна быть ограничена"


def test_ui_has_persist_toggle():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="strikePersist"' in html, "нет тумблера настойчивой эскалации в UI"
    assert "_payload.persist" in html, "strikeLaunch должен передавать persist"


async def _no_accounts(*a, **k):
    return []


async def _passthrough_quar(pool, op_id, viable):
    return (viable, 0)


@pytest.mark.asyncio
async def test_legal_only_persist_continues_without_accounts(monkeypatch):
    """Заход без аккаунтов (SMTP есть) = чистые юр-письма. Верифицировать нечем
    (verified_down None), но флот не жжётся → давление письмами продолжается до
    лимита. Иначе цепочка обрывалась после первого письма."""
    from services.strike_engine import StrikeResult
    from tests.test_executors import FakePool

    monkeypatch.setattr("services.resource_selector.select_all_active", _no_accounts)
    monkeypatch.setattr(op_worker, "_filter_quarantined_accounts", _passthrough_quar)

    async def _fake_stag(plan, **k):
        # legal-only: писем отправили, но проверить снятие некому → None
        return [StrikeResult(target="@bad", verified_down=None)]
    monkeypatch.setattr("services.strike_engine.staggered_strike", _fake_stag)

    cont = {}

    async def _fake_cont(pool, owner_id, params, chain):
        cont["chain"] = chain
        return 555
    monkeypatch.setattr(op_worker, "_schedule_strike_continuation", _fake_cont)

    pool = FakePool(fetch=[], fetchrow={"mode": "normal"}, fetchval=2)  # 2 SMTP
    res = await op_worker._exec_strike(
        pool, None, 1, 10,
        {"target": "@bad", "reason": "drugs", "force": True, "persist": True, "strike_chain": 0})
    assert res["status"] != "failed"
    assert cont.get("chain") == 1, (
        "в legal-only режиме юр-письма должны продолжаться без аккаунтов/верификации"
    )


@pytest.mark.asyncio
async def test_with_accounts_unverified_does_not_continue_blind(monkeypatch):
    """С флотом и НЕ подтверждённым снятием (None) — не гоним вслепую (бережём
    аккаунты). Строгая логика сохраняется, послабление только для legal-only."""
    from services.strike_engine import StrikeResult
    from tests.test_executors import FakePool

    async def _one_acc(*a, **k):
        return [{"id": 1, "session_str": "s", "trust_score": 0.9,
                 "is_active": True, "acc_status": "active"}]
    monkeypatch.setattr("services.resource_selector.select_all_active", _one_acc)
    monkeypatch.setattr(op_worker, "_filter_quarantined_accounts", _passthrough_quar)

    async def _fake_stag(plan, **k):
        return [StrikeResult(target="@bad", verified_down=None)]
    monkeypatch.setattr("services.strike_engine.staggered_strike", _fake_stag)

    cont = {}

    async def _fake_cont(pool, owner_id, params, chain):
        cont["called"] = True
        return 555
    monkeypatch.setattr(op_worker, "_schedule_strike_continuation", _fake_cont)

    pool = FakePool(fetch=[], fetchrow={"mode": "normal"}, fetchval=2)
    await op_worker._exec_strike(
        pool, None, 1, 10,
        {"target": "@bad", "reason": "drugs", "force": True, "persist": True, "strike_chain": 0})
    assert not cont.get("called"), (
        "с аккаунтами и неподтверждённым снятием не продолжаем вслепую"
    )
