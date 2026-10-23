"""Регрессия: Strike «не уничтожал цели» — движок не проверял результат.

Жалоба пользователя: «Система Strike не уничтожает цели в отличии от софтов
конкурентов». Причина найдена в коде: функция verify_target_takedown была
полностью написана (проверяет, доступна ли цель после атаки), но НИГДЕ не
вызывалась — мёртвый код. Из-за этого StrikeResult.verified_down всегда оставался
None, и строка сводки «🔍 Проверка: ✅ УДАЛЁН / ⚠️ Всё ещё активен» никогда не
показывалась. Пользователь запускал страйк, движок отрабатывал все векторы, но
исход («убил цель или нет») не проверялся и не сообщался — ровно ощущение
«не уничтожает цели»: ни подтверждения, ни сигнала «цель жива, нужен повтор».

Фикс: staggered_strike вызывает verify_target_takedown финальной фазой (после всех
волн и эскалаций) и кладёт результат в result.verified_down. None остаётся ТОЛЬКО
если проверить не удалось — не выдаём за успех.

Тесты падают без фикса: без вызова verify verified_down == None.
"""
from __future__ import annotations

import inspect

import pytest

from services import strike_engine
from services.strike_engine import StrikePlan, staggered_strike


def test_verify_takedown_is_actually_called_in_staggered_strike():
    """Статическая страховка от повторного «омертвления» верификации: без вызова
    в staggered_strike вся фаза бесполезна, а поведенческий тест можно обойти."""
    src = inspect.getsource(staggered_strike)
    assert "verify_target_takedown(" in src, (
        "staggered_strike обязан вызывать verify_target_takedown — иначе исход "
        "атаки не проверяется (жалоба «Strike не уничтожает цели»)"
    )
    # результат должен ложиться именно в verified_down
    assert "verified_down = await verify_target_takedown" in src.replace(
        "result.", ""
    ), "результат верификации должен попадать в result.verified_down"


@pytest.mark.asyncio
async def test_verified_down_true_when_target_removed(monkeypatch):
    """Цель после атаки недоступна → verified_down=True (подтверждённое удаление)."""
    # Мгновенные паузы — иначе staggered_strike спит минутами (staggers/cooldowns).
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(strike_engine.asyncio, "sleep", _no_sleep)

    # Один аккаунт «отрабатывает» атаку успешно.
    async def _fake_strike(*a, **k):
        return {"peer_reported": True, "multi_reason_sent": 1, "joined": True}
    monkeypatch.setattr(
        "services.account_manager.report_peer_deep_v2", _fake_strike, raising=False
    )
    # SpamBot-эскалация не должна лезть в сеть.
    async def _fake_spambot(acc, target):
        return {"status": "skipped", "bots": {}}
    monkeypatch.setattr(strike_engine, "_escalate_to_spambot", _fake_spambot)
    # Верификация: цель удалена.
    async def _verify_down(acc, target, **k):
        return True
    monkeypatch.setattr(strike_engine, "verify_target_takedown", _verify_down)

    plan = StrikePlan(
        targets=["@victim"],
        accounts=[{"id": 1, "session_str": "s1", "trust_score": 0.9, "is_active": True}],
        reason="spam",
        preset=None,
        label="t",
        waves=[[{"id": 1, "session_str": "s1", "trust_score": 0.9, "is_active": True}]],
    )
    results = await staggered_strike(plan)
    assert len(results) == 1
    assert results[0].verified_down is True, (
        "цель недоступна после атаки → verified_down должен быть True"
    )


@pytest.mark.asyncio
async def test_verified_down_false_when_target_survives(monkeypatch):
    """Цель после атаки жива → verified_down=False (честно: удар не добил)."""
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(strike_engine.asyncio, "sleep", _no_sleep)

    async def _fake_strike(*a, **k):
        return {"peer_reported": True}
    monkeypatch.setattr(
        "services.account_manager.report_peer_deep_v2", _fake_strike, raising=False
    )

    async def _fake_spambot(acc, target):
        return {"status": "skipped", "bots": {}}
    monkeypatch.setattr(strike_engine, "_escalate_to_spambot", _fake_spambot)

    async def _verify_alive(acc, target, **k):
        return False
    monkeypatch.setattr(strike_engine, "verify_target_takedown", _verify_alive)

    plan = StrikePlan(
        targets=["@survivor"],
        accounts=[{"id": 2, "session_str": "s2", "trust_score": 0.8, "is_active": True}],
        reason="spam",
        preset=None,
        label="t",
        waves=[[{"id": 2, "session_str": "s2", "trust_score": 0.8, "is_active": True}]],
    )
    results = await staggered_strike(plan)
    assert results[0].verified_down is False, (
        "цель ещё активна → verified_down=False (не выдаём за успех)"
    )
    # И сводка должна честно это показать.
    summary = strike_engine.format_strike_summary(results)
    assert "Всё ещё активен" in summary, "пользователь должен видеть, что цель выжила"


@pytest.mark.asyncio
async def test_verify_failure_leaves_none_not_false(monkeypatch):
    """Сбой самой проверки (сеть/сессия) → verified_down остаётся None
    («не проверено»), а не False — иначе честный «не знаю» превратился бы в
    ложное «цель выжила»."""
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(strike_engine.asyncio, "sleep", _no_sleep)

    async def _fake_strike(*a, **k):
        return {"peer_reported": True}
    monkeypatch.setattr(
        "services.account_manager.report_peer_deep_v2", _fake_strike, raising=False
    )

    async def _fake_spambot(acc, target):
        return {"status": "skipped", "bots": {}}
    monkeypatch.setattr(strike_engine, "_escalate_to_spambot", _fake_spambot)

    async def _verify_boom(acc, target, **k):
        raise RuntimeError("проверка недоступна")
    monkeypatch.setattr(strike_engine, "verify_target_takedown", _verify_boom)

    plan = StrikePlan(
        targets=["@unknown"],
        accounts=[{"id": 3, "session_str": "s3", "trust_score": 0.7, "is_active": True}],
        reason="spam",
        preset=None,
        label="t",
        waves=[[{"id": 3, "session_str": "s3", "trust_score": 0.7, "is_active": True}]],
    )
    results = await staggered_strike(plan)
    assert results[0].verified_down is None, (
        "сбой проверки = None (не проверено), а не False"
    )
