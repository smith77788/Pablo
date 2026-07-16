"""Организм заживает сам: истёкший кулдаун снимается автоматически.

op_worker при сетевом/прокси-сбое ставит acc_status='cooldown' + cooldown_until
(+15 мин), но ничто не возвращало статус в 'active' после истечения окна
(reactivate в check_accounts_health бьёт только по is_active=FALSE). В итоге один
FloodWait 15 минут назад держал аккаунт в «⚠️ Под риском» бесконечно, пока
пользователь не запустит проверку вручную.

Фикс двухслойный:
  1. Пульс (get_account_health) считает 'cooldown' риском ТОЛЬКО пока окно
     активно (cd_active) — UI перестаёт врать сразу.
  2. account_monitor._heal_expired_cooldowns чистит persisted-статус на цикле —
     чтобы и другие потребители acc_status ('cooldown') увидели 'active'.
"""
from __future__ import annotations

import asyncio

from services.infra_memory import get_account_health


class _FakePool:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []

    async def fetch(self, q, *a):
        return self._rows

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "UPDATE 3"


def test_expired_cooldown_not_risk_in_pulse():
    async def _run():
        rows = [
            # активное окно кулдауна → риск
            {"id": 1, "phone": "a", "acc_status": "cooldown", "trust_score": 1.0,
             "cd_active": True, "restrictions": 0, "severe": 0, "floods": 0},
            # окно истекло → здоров (не риск)
            {"id": 2, "phone": "b", "acc_status": "cooldown", "trust_score": 1.0,
             "cd_active": False, "restrictions": 0, "severe": 0, "floods": 0},
        ]
        r = await get_account_health(_FakePool(rows=rows), 7)
        by_id = {a["account_id"]: a for a in r["accounts"]}
        assert by_id[1]["status"] == "at_risk", "активный кулдаун — риск"
        assert by_id[2]["status"] == "healthy", "истёкший кулдаун — здоров"
        assert r["summary"]["at_risk"] == 1 and r["summary"]["healthy"] == 1
    asyncio.run(_run())


def test_heal_sweep_targets_only_expired_cooldown():
    async def _run():
        from services import account_monitor
        pool = _FakePool()
        await account_monitor._heal_expired_cooldowns(pool)
        assert pool.executed, "sweep должен выполнить UPDATE"
        q = pool.executed[0][0]
        assert "acc_status='active'" in q, "должен снимать статус в active"
        assert "acc_status='cooldown'" in q, "только для cooldown-аккаунтов"
        assert "is_active=TRUE" in q, "только для включённых"
        assert "cooldown_until" in q and "NOW()" in q, "только для истёкшего окна"
        # 'warming'/'banned'/'session_expired' НЕ трогаем — их нет в условии
        assert "warming" not in q and "session_expired" not in q
    asyncio.run(_run())


def test_heal_sweep_failsafe_on_db_error():
    async def _run():
        from services import account_monitor

        class _BoomPool:
            async def execute(self, q, *a):
                raise RuntimeError("db down")
        # не должно пробрасывать исключение (fail-soft, монитор продолжает цикл)
        await account_monitor._heal_expired_cooldowns(_BoomPool())
    asyncio.run(_run())
