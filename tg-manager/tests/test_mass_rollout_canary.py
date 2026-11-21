"""Канарейка массовых возможностей (services/mass_rollout.py).

Пакетный инвайт и горизонтальный исполнитель готовы, но не обкатаны на живом
Telegram. Раньше у них был только рубильник «всем сразу». Здесь проверяется:

* без настроек ничего не включается — поведение прежнее;
* выключатель сильнее всего, включая явный bulk_api=True у операции;
* доля операций стабильна по op_id и примерно равна заданной;
* предохранитель выключает пакет после серии флудов/откатов и сам
  включается после остывания;
* потолок не-лидера по умолчанию 1, 0 — «слить», и он доходит до отбора.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest
from services import mass_rollout as mr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENV = ("INVITE_BULK_API", "INVITE_BULK_KILL", "INVITE_BULK_CANARY_PCT",
        "INVITE_BULK_CANARY_OWNERS", "INVITE_BULK_MAX_BATCH",
        "OP_SHARDED_FOLLOWER_MAX_PARALLEL", "INVITE_BULK_BREAKER_FLOODS",
        "INVITE_BULK_BREAKER_COOLDOWN_SEC")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    mr._reset_for_tests()
    yield
    mr._reset_for_tests()


class _Pool:
    def __init__(self, settings=None, fail=False):
        self.settings = dict(settings or {})
        self.fail = fail
        self.calls = 0

    async def fetchrow(self, sql, key):
        self.calls += 1
        if self.fail:
            raise OSError("db down")
        assert "platform_settings" in sql
        return {"value": self.settings[key]} if key in self.settings else None


def _decide(pool, op_id=1, owner=10, explicit=None):
    return asyncio.run(mr.bulk_invite_decision(pool, op_id, owner, explicit))


def test_nothing_is_enabled_by_default():
    for op in range(200):
        assert _decide(_Pool(), op_id=op) == (False, "off")


def test_kill_beats_explicit_and_global(monkeypatch):
    monkeypatch.setenv("INVITE_BULK_API", "1")
    assert _decide(_Pool({"invite_bulk_kill": "1"}), explicit=True) == (False, "kill")
    mr._reset_for_tests()
    monkeypatch.setenv("INVITE_BULK_KILL", "1")
    assert _decide(_Pool(), explicit=True) == (False, "kill")


def test_explicit_and_global_and_owner(monkeypatch):
    assert _decide(_Pool(), explicit=True) == (True, "explicit")
    assert _decide(_Pool(), explicit=False) == (False, "explicit")
    monkeypatch.setenv("INVITE_BULK_CANARY_OWNERS", "7, 10")
    assert _decide(_Pool(), owner=10) == (True, "canary_owner")
    assert _decide(_Pool(), owner=11) == (False, "off")
    monkeypatch.setenv("INVITE_BULK_API", "1")
    assert _decide(_Pool(), owner=11) == (True, "global")


def test_pct_is_stable_and_close_to_share(monkeypatch):
    monkeypatch.setenv("INVITE_BULK_CANARY_PCT", "10")
    first = [_decide(_Pool(), op_id=i)[0] for i in range(2000)]
    again = [_decide(_Pool(), op_id=i)[0] for i in range(2000)]
    assert first == again, "одна операция не должна скакать между путями"
    share = sum(first) / len(first)
    assert 0.07 < share < 0.13, share


def test_db_pct_overrides_env_without_deploy(monkeypatch):
    monkeypatch.setenv("INVITE_BULK_CANARY_PCT", "100")
    assert _decide(_Pool({"invite_bulk_canary_pct": "0"}))[0] is False


def test_db_failure_keeps_last_known_kill():
    pool = _Pool({"invite_bulk_kill": "1"})
    assert _decide(pool, explicit=True)[1] == "kill"
    mr._settings_cache["invite_bulk_kill"] = ("1", -1e9)  # кэш устарел
    pool.fail = True
    assert _decide(pool, explicit=True)[1] == "kill", \
        "блип базы не должен снимать выключатель"


def test_breaker_trips_on_floods_and_cools_down(monkeypatch):
    monkeypatch.setenv("INVITE_BULK_BREAKER_FLOODS", "3")
    for _ in range(2):
        mr.record_bulk_batch("peer_flood")
    assert not mr.bulk_breaker_open()
    mr.record_bulk_batch("peer_flood")
    assert mr.bulk_breaker_open()
    mr._open_until = 0.0  # остывание прошло
    assert not mr.bulk_breaker_open()


def test_breaker_trips_on_fallback_share():
    for _ in range(3):
        mr.record_bulk_batch("ok")
    for _ in range(3):
        mr.record_bulk_batch("fallback")
    assert mr.bulk_breaker_open()


def test_breaker_stays_closed_on_healthy_traffic():
    for _ in range(100):
        mr.record_bulk_batch("ok")
    assert not mr.bulk_breaker_open()


def test_follower_cap_defaults_to_one_and_drains_on_zero(monkeypatch):
    assert asyncio.run(mr.sharded_follower_cap(_Pool(), 8)) == 1
    mr._reset_for_tests()
    assert asyncio.run(mr.sharded_follower_cap(
        _Pool({"op_sharded_follower_max_parallel": "0"}), 8)) == 0
    mr._reset_for_tests()
    monkeypatch.setenv("OP_SHARDED_FOLLOWER_MAX_PARALLEL", "99")
    assert asyncio.run(mr.sharded_follower_cap(_Pool(), 8)) == 8


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_wiring_reaches_real_paths():
    """Канарейка обязана стоять на настоящем пути, а не рядом с ним."""
    ow = _src("services/op_worker.py")
    assert "_rollout.bulk_invite_decision(" in ow
    follower = ow[ow.index("async def _follower_round("):]
    follower = follower[:follower.index("\nasync def ", 10)]
    assert re.search(r"_process_pending\(pool, bot, slot_cap=_cap\)", follower)
    eng = _src("services/mass_inviter_engine.py")
    assert "_rollout.bulk_breaker_open()" in eng
    assert "_rollout.record_bulk_batch(" in eng
    assert "_rollout.bulk_max_batch()" in eng


def test_kill_reaches_running_operation_per_batch():
    """Решение принимается на старте, но выключатель — на каждом батче."""
    ow = _src("services/op_worker.py")
    assert "bulk=(_bulk_api and not await _rollout.bulk_killed(pool))" in ow
    assert asyncio.run(mr.bulk_killed(_Pool({"invite_bulk_kill": "on"}))) is True
    mr._reset_for_tests()
    assert asyncio.run(mr.bulk_killed(_Pool())) is False
