"""Экран флота обязан знать ВСЁ, из-за чего операция не берёт аккаунт.

ЧТО БЫЛО. Пульс флота сделан отвечать на вопрос владельца «из 52 аккаунтов
работают 20, а почему непонятно». Знал он при этом три причины: мёртвый статус,
кулдаун и карантин риск-пульса. Дверь выбора аккаунтов
(`resource_selector.select_all_active`) отсеивала ещё четыре:

  * `session_str IS NULL` — аккаунт без сессии не берёт ни одна операция;
  * мёртвый прокси (`split_by_dead_proxy`) — сети до Telegram нет, каждая
    попытка падает сетевой ошибкой;
  * исчерпанный дневной лимит действий (`respect_daily_budget`);
  * доверие ниже порога операции (`flood_engine.min_trust_for_action`).

Про такие аккаунты экран писал «✅ готов», а сводка — «🎉 Весь флот готов к
действиям — пауз нет». Владелец запускал операцию и получал «обработано 0»:
ровно то непонимание, на которое экран и сделан отвечать, только теперь с
подтверждением от самого продукта, что всё в порядке.

ЧТО ТЕПЕРЬ. Экран считает состояние по тем же фактам, что дверь выбора.
Починка руками (нет сессии, мёртвый прокси) — отдельное состояние `blocked`:
это не пауза (ждать нечего) и не смерть (аккаунт цел). Дневной лимит — пауза со
сроком до полуночи UTC. Доверие — оговорка у готового аккаунта (`hint`): часть
операций его возьмёт, инвайты и рассылки — нет.
"""
from __future__ import annotations

import os

import pytest

from services import fleet_pulse as fp
from services import flood_engine as fe

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


class _Pool:
    """Пул, различающий запросы экрана по SQL, а не по порядку вызовов."""

    def __init__(self, accounts: list[dict], actions_today: dict[int, int] | None = None):
        self.accounts = accounts
        self.actions_today = actions_today or {}

    async def fetch(self, sql, *args):
        if "account_rehab_state" in sql:
            return []
        if "restriction_events" in sql:
            return []
        if "operation_audit" in sql:
            return [{"account_id": i, "n": n}
                    for i, n in self.actions_today.items()]
        return [dict(a) for a in self.accounts]

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        return None


def _acc(acc_id: int, **kw) -> dict:
    row = {"id": acc_id, "phone": f"+7000000{acc_id:04d}", "first_name": "A",
           "username": None, "acc_status": "active", "cooldown_until": None,
           "trust_score": 0.9, "has_session": True, "proxy_alive": None,
           "proxy_fail_streak": 0}
    row.update(kw)
    return row


async def test_account_without_a_session_is_not_called_ready():
    """Падало: без сессии аккаунт не берёт никто, а экран звал его готовым."""
    states = await fp.account_states(_Pool([_acc(1, has_session=False)]), 77)
    assert states[0]["state"] == "blocked", states
    assert "сесси" in states[0]["reason"].lower()
    assert states[0]["ready_in_sec"] is None, "ждать тут нечего"


async def test_account_on_a_dead_proxy_is_not_called_ready():
    """Падало: дверь выбора такой аккаунт выбрасывает, экран — нет."""
    dead = _acc(2, proxy_alive=False, proxy_fail_streak=_dead_streak())
    states = await fp.account_states(_Pool([dead]), 77)
    assert states[0]["state"] == "blocked", states
    assert "прокси" in states[0]["reason"].lower()


def _dead_streak() -> int:
    from services.resource_selector import PROXY_DEAD_STREAK
    return PROXY_DEAD_STREAK


async def test_a_blinking_proxy_is_not_a_blocker():
    """Одна-две неудачи — моргание, а не смерть: порог берём у двери."""
    blink = _acc(3, proxy_alive=False, proxy_fail_streak=_dead_streak() - 1)
    states = await fp.account_states(_Pool([blink]), 77)
    assert states[0]["state"] == "ready", states


async def test_account_without_a_proxy_at_all_is_ready():
    """Работа с IP хоста — законный режим, а не поломка."""
    states = await fp.account_states(_Pool([_acc(4)]), 77)
    assert states[0]["state"] == "ready", states


async def test_exhausted_daily_budget_is_a_pause_with_a_deadline():
    """Падало: исчерпавший лимит аккаунт дверь не отдаёт, экран звал готовым."""
    from services import account_budget as ab

    pool = _Pool([_acc(5)], actions_today={5: ab.DEFAULT_DAILY_BUDGET + 1})
    states = await fp.account_states(pool, 77)
    assert states[0]["state"] == "cooling", states
    assert "лимит" in states[0]["reason"].lower()
    assert states[0]["ready_in_sec"] and states[0]["ready_in_sec"] <= 24 * 3600


async def test_low_trust_is_named_without_claiming_a_pause():
    """Аккаунт рабочий, но инвайт и рассылка его не возьмут — скажем это."""
    need = fe.min_trust_for_action("invite")
    states = await fp.account_states(_Pool([_acc(6, trust_score=need - 0.1)]), 77)
    assert states[0]["state"] == "ready", "это не пауза: часть операций его берёт"
    assert states[0]["hint"], "оговорка обязана быть названа"
    assert "инвайт" in states[0]["hint"]


async def test_trusted_account_gets_no_hint():
    states = await fp.account_states(_Pool([_acc(7, trust_score=1.0)]), 77)
    assert states[0]["hint"] is None, states


async def test_hundred_percent_trust_scale_is_understood():
    """trust_score в продукте бывает и 0..100 — нормализацию берём у двери."""
    states = await fp.account_states(_Pool([_acc(8, trust_score=95)]), 77)
    assert states[0]["hint"] is None, states


# ── Сводка и экран ─────────────────────────────────────────────────────────

def test_summary_counts_the_new_state():
    s = fp.summarize([{"state": "blocked"}, {"state": "ready"}])
    assert s["blocked"] == 1 and s["ready"] == 1 and s["total"] == 2


def test_screen_does_not_promise_a_ready_fleet_over_a_caveat():
    """«Весь флот готов» перед «обработано 0» — худшее, что экран может сказать."""
    from bot.handlers import fleet_pulse as screen

    limited = [{"state": "ready", "state_label": "✅ готов", "name": "acc",
                "reason": "готов к действиям", "ready_in": None,
                "hint": "доверие 0.40 ниже 0.50 — инвайты и рассылки "
                        "этот аккаунт не возьмут"}]
    text = screen._render(limited)
    assert "Весь флот готов" not in text
    assert "инвайты и рассылки" in text


def test_screen_still_celebrates_a_truly_clean_fleet():
    from bot.handlers import fleet_pulse as screen

    clean = [{"state": "ready", "state_label": "✅ готов", "name": "acc",
              "reason": "готов к действиям", "ready_in": None, "hint": None}]
    assert "Весь флот готов" in screen._render(clean)


def test_screen_counts_accounts_needing_repair():
    from bot.handlers import fleet_pulse as screen

    states = [{"state": "blocked", "state_label": "🔧 нужна починка",
               "name": "acc", "reason": "нет сессии — переимпортируйте аккаунт",
               "ready_in": None, "hint": None}]
    text = screen._render(states)
    assert "нужна починка: <b>1</b>" in text
    assert "нет сессии" in text


# ── Храповик: экран и дверь судят по одному предикату прокси ───────────────

def test_proxy_verdict_comes_from_the_selection_door():
    import inspect

    src = inspect.getsource(fp._proxy_is_dead)
    assert "resource_selector" in src, (
        "экран судит о прокси сам — он разъедется с дверью выбора")
    assert "PROXY_DEAD_STREAK" not in src, "порог тоже берём у двери, не числом"


# ── Живая БД: кого дверь не берёт, того экран не зовёт готовым ─────────────

@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
async def test_screen_and_door_agree_on_every_cause():
    """Инвариант: ни один не выбранный дверью аккаунт не «готов» на экране.

    Заглушки согласие не ловят: у экрана и у двери свои. Здесь оба читают одни
    строки, и причины перечислены по одной на аккаунт.
    """
    import asyncpg

    from services import resource_selector as rs

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    owner = 918273646
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            await conn.execute("DELETE FROM user_proxies WHERE owner_id=$1", owner)
            dead_proxy = await conn.fetchval(
                "INSERT INTO user_proxies (owner_id, proxy_url, is_active, "
                "is_alive, consecutive_failures) "
                "VALUES ($1, $2, TRUE, FALSE, $3) RETURNING id",
                owner, "socks5://dead:1080", rs.PROXY_DEAD_STREAK + 1)
            ids: dict[str, int] = {}
            ids["ok"] = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, "
                "acc_status, is_active, trust_score) "
                "VALUES ($1,$2,'x','active',TRUE,1.0) RETURNING id",
                owner, "+70000001001")
            ids["banned"] = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, "
                "acc_status, is_active, trust_score) "
                "VALUES ($1,$2,'x','banned',TRUE,1.0) RETURNING id",
                owner, "+70000001002")
            ids["dead_proxy"] = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, "
                "acc_status, is_active, trust_score, proxy_id) "
                "VALUES ($1,$2,'x','active',TRUE,1.0,$3) RETURNING id",
                owner, "+70000001003", dead_proxy)
            ids["cooldown"] = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, "
                "acc_status, is_active, trust_score, cooldown_until) "
                "VALUES ($1,$2,'x','active',TRUE,1.0, now() + interval '2 hours') "
                "RETURNING id",
                owner, "+70000001004")

        picked = {int(r["id"]) for r in
                  await rs.select_all_active(pool, owner, min_trust_score=0.0)}
        screen = {s["id"]: s for s in await fp.account_states(pool, owner)}

        assert ids["ok"] in picked and screen[ids["ok"]]["state"] == "ready"
        for label in ("banned", "dead_proxy", "cooldown"):
            acc = ids[label]
            assert acc not in picked, f"{label}: дверь всё-таки берёт аккаунт"
            assert screen[acc]["state"] != "ready", (
                f"{label}: дверь не берёт, а экран зовёт готовым — "
                "это и есть «работают 20 из 52, а почему непонятно»")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            await conn.execute("DELETE FROM user_proxies WHERE owner_id=$1", owner)
        await pool.close()
