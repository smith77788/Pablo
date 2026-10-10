"""Диагностика обещала владельцу числа, которых не считала.

Две находки одного класса — «экран говорит число, а под ним другой вопрос».

1. `fleet_doctor` строит воронку отбора, повторяя фильтры
   `resource_selector.select_all_active`, и называет шаг, где флот обнуляется.
   Список мёртвых статусов у него был свой и короче общего: аккаунт со
   статусом `deleted` или `frozen` доктор считал живым, шаг «статус» рисовал
   зелёным — и владелец читал «Флот готов» ровно там, где операция получит
   ноль целей. Доктор, который не видит болезнь, хуже отсутствия доктора:
   он отводит от причины.

2. `infra_advisor` считал подошедшие под правило аккаунты как `len(rows)`,
   а в запросах стоял `LIMIT 5` / `LIMIT 10`. «Низкое доверие: 5 аккаунт(ов)»
   на флоте из двухсот означало «пять или больше», а владелец принимает по
   этому числу решение лечить флот или нет. Плюс карточка «Проблемные
   аккаунты» смотрела только на десять самых старых аккаунтов и советовала
   «Очистите их» — то есть выбросить аккаунт, у которого всего лишь отозвана
   сессия.
"""
from __future__ import annotations

import re
from pathlib import Path

from services import account_status, fleet_doctor, infra_advisor

ROOT = Path(__file__).resolve().parent.parent
ADVISOR_SRC = (ROOT / "services" / "infra_advisor.py").read_text(encoding="utf-8")


def _acc(**kw):
    row = {"is_active": True, "has_session": True, "acc_status": "active",
           "cd_active": False, "trust_score": 1.0, "in_operation": False}
    row.update(kw)
    return row


# ── 1. Воронка доктора и дверь отбора говорят об одном словаре ──────────────

def test_funnel_uses_the_shared_death_vocabulary():
    assert fleet_doctor._DEAD == set(account_status.DEAD_STATUSES)


def test_every_dead_status_drops_out_of_the_funnel():
    """Ни один статус из словаря не проходит шаг «статус»."""
    for status in sorted(account_status.DEAD_STATUSES):
        f = fleet_doctor.compute_funnel([_acc(acc_status=status)])
        assert f["with_session"] == 1, status
        assert f["alive"] == 0, f"{status} прошёл шаг «статус» как живой"


def test_dead_account_is_named_with_a_russian_reason():
    """Владелец читает причину по-русски, а не код статуса из базы."""
    for status in sorted(account_status.DEAD_STATUSES):
        blocked = fleet_doctor.step_blockers([_acc(acc_status=status)], "alive")
        assert len(blocked) == 1, status
        reason = blocked[0]["reason"]
        assert reason and reason != status, f"нет русской подписи для {status}"
        # «Telegram» — имя собственное, его владелец читает и так.
        assert not re.search(r"[A-Za-z]", reason.replace("Telegram", "")), (
            f"английский в причине: {reason}")


def test_the_probe_would_notice_the_narrow_list_coming_back():
    """Самопроверка: на прежнем коротком списке тест обязан падать."""
    narrow = {"banned", "spamblock", "deactivated", "session_expired"}
    missed = [s for s in account_status.DEAD_STATUSES if s not in narrow]
    assert missed, "словарь сузился до прежнего списка — пробник ослеп"
    saved = fleet_doctor._DEAD
    try:
        fleet_doctor._DEAD = narrow
        f = fleet_doctor.compute_funnel([_acc(acc_status=missed[0])])
        assert f["alive"] == 1, "короткий список обязан пропускать мёртвого"
    finally:
        fleet_doctor._DEAD = saved


# ── 2. Карточки советчика считают весь флот, а не свой LIMIT ────────────────

class _Pool:
    """Заглушка пула: отдаёт строки по порядку запросов и считает окно сама."""

    def __init__(self, accounts):
        self.accounts = accounts

    async def fetch(self, sql, *args):
        if "cooldown_until > NOW()" in sql:
            return self._limited([a for a in self.accounts if a.get("cooling")], 5)
        if "flood_count_7d,0) > 10" in sql:
            return self._limited([a for a in self.accounts if a.get("floody")], 5)
        if "trust_score,1.0) < 0.3" in sql:
            return self._limited([a for a in self.accounts if a.get("low_trust")], 5)
        if "acc_status" in sql and "session_str" in sql:
            dead = account_status.DEAD_STATUSES
            return [dict(a) for a in self.accounts
                    if a.get("acc_status") in dead or not a.get("has_session", True)]
        return []

    @staticmethod
    def _limited(rows, limit):
        """Как Postgres: окно COUNT(*) OVER () считается ДО LIMIT."""
        return [dict(r, match_cnt=len(rows)) for r in rows[:limit]]

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM tg_accounts" in sql:
            return len(self.accounts)
        return 0

    async def fetchrow(self, sql, *args):
        return None


def _accounts(n, **flags):
    return [{"id": i, "phone": f"+7000{i}", "first_name": f"acc{i}",
             "acc_status": "active", "has_session": True, **flags}
            for i in range(1, n + 1)]


async def _recs(accounts):
    return await infra_advisor.get_recommendations(_Pool(accounts), 1)


async def test_low_trust_card_names_the_real_number_not_the_limit():
    recs = await _recs(_accounts(40, low_trust=True))
    card = next(r for r in recs if "Низкое доверие" in r["title"])
    assert "40" in card["title"], card["title"]


async def test_long_cooldown_card_names_the_real_number():
    recs = await _recs(_accounts(23, cooling=True))
    card = next(r for r in recs if "Долгий кулдаун" in r["title"])
    assert "23" in card["title"], card["title"]


async def test_flood_card_names_the_real_number():
    accounts = _accounts(12, floody=True)
    for a in accounts:
        a["flood_count_7d"] = 15
    recs = await _recs(accounts)
    card = next(r for r in recs if "флуд-активность" in r["title"])
    assert "12" in card["title"], card["title"]


async def test_no_card_counts_rows_of_a_limited_query():
    """Храповик: число в заголовке не берётся из len() обрезанной выборки."""
    for name in ("cooling_long", "flood_heavy", "low_trust", "poor_memory"):
        assert f"len({name})" not in ADVISOR_SRC, (
            f"{name} снова считается длиной выборки с LIMIT")


async def test_broken_accounts_card_sees_every_dead_status():
    """Аккаунт с deleted/frozen числится активным — карточка обязана его назвать."""
    for status in sorted(account_status.DEAD_STATUSES):
        accounts = _accounts(1)
        accounts[0]["acc_status"] = status
        recs = await _recs(accounts)
        assert any("Проблемные аккаунты" in r["title"] for r in recs), status


async def test_broken_accounts_card_sees_a_missing_session():
    accounts = _accounts(1)
    accounts[0]["has_session"] = False
    recs = await _recs(accounts)
    assert any("Проблемные аккаунты" in r["title"] for r in recs)


async def test_broken_accounts_card_does_not_tell_to_throw_away_a_revivable_one():
    """Совет зависит от того, возвращается аккаунт или нет."""
    accounts = _accounts(2)
    accounts[0]["acc_status"] = "session_expired"
    accounts[1]["acc_status"] = "spamblock"
    recs = await _recs(accounts)
    card = next(r for r in recs if "Проблемные аккаунты" in r["title"])
    assert "2" in card["title"]
    assert "переподключите" in card["text"]
    assert "удалять не нужно" in card["text"]
    assert "Очистите их" not in card["text"]


async def test_broken_accounts_card_still_says_to_drop_the_lost_ones():
    accounts = _accounts(1)
    accounts[0]["acc_status"] = "banned"
    recs = await _recs(accounts)
    card = next(r for r in recs if "Проблемные аккаунты" in r["title"])
    assert "безвозвратно" in card["text"]


async def test_a_healthy_fleet_gets_no_broken_accounts_card():
    """Самопроверка пробника: на здоровом флоте карточки быть не должно."""
    recs = await _recs(_accounts(5))
    assert not any("Проблемные аккаунты" in r["title"] for r in recs)
