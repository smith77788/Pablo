"""Мёртвый статус аккаунта — один словарь на весь продукт.

ЧТО БЫЛО. Набор «мёртвых» значений `tg_accounts.acc_status` выписывал каждый
читатель сам, и наборы разъехались. Дверь выбора аккаунтов
(`resource_selector`) и экран флота (`fleet_pulse`) считали мёртвым
`spamblock`, а риск-пульс (`infra_memory.get_account_health`) — нет. Поэтому
аккаунт, которому `flood_engine` поставил `spamblock` после PEER_FLOOD,
показывался владельцу на приборном щитке ЗДОРОВЫМ: операции его уже не брали,
экран флота называл выбывшим, а щиток — «здоров». Первые семь дней его ещё
спасал счётчик флудов в том же пульсе, потом он уезжал в «здоров» навсегда.
Читается это как «из 52 аккаунтов работают 20, а почему — непонятно»: именно
то место, которое сделано отвечать на этот вопрос, отвечало неправдой.

ЧТО ТЕПЕРЬ. Набор живёт в одном месте — `account_status.DEAD_STATUSES`, и все
четыре органа читают его через `is_dead()` / `sql_dead_list()`. Набор — union
всех прежних, то есть при сведении защита не ослабла: `deleted` и `frozen`
теперь мёртвые и для выбора, и для пульса.
"""
from __future__ import annotations

import ast
import inspect
import os

import pytest

from services import account_status as acc_status
from services import fleet_pulse, immunity_engine, infra_memory, resource_selector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


# ── Сам словарь ────────────────────────────────────────────────────────────

def test_dead_set_is_the_union_of_what_every_organ_knew():
    """Сведение не имеет права ослабить ни одну из прежних проверок."""
    for status in ("banned", "deactivated", "session_expired",  # дверь выбора
                   "spamblock",                                  # экран флота
                   "deleted", "frozen"):                         # пульс/иммунитет
        assert acc_status.is_dead(status), status
    for status in ("active", "ok", "cooldown", "warming", None, ""):
        assert not acc_status.is_dead(status), status


def test_sql_list_quotes_only_known_statuses():
    """`sql_dead_list` подставляется в текст запроса — там только свои литералы."""
    sql = acc_status.sql_dead_list()
    quoted = {part.strip().strip("'") for part in sql.split(",")}
    assert quoted == set(acc_status.DEAD_STATUSES)
    assert '"' not in sql and ";" not in sql and "--" not in sql


def test_immunity_reads_the_same_set():
    assert immunity_engine.DEATH_STATUSES is acc_status.DEAD_STATUSES


# ── Риск-пульс ─────────────────────────────────────────────────────────────

class _HealthPool:
    """Пул риск-пульса: ни ограничений, ни флудов — только acc_status."""

    def __init__(self, statuses: list[str]):
        self.statuses = statuses

    async def fetch(self, sql, *args):
        return [
            {"id": 1000 + i, "phone": f"+7000000{i:04d}", "acc_status": s,
             "trust_score": 1.0, "cd_active": False,
             "restrictions": 0, "severe": 0, "floods": 0}
            for i, s in enumerate(self.statuses)
        ]


@pytest.mark.parametrize("status", sorted(acc_status.DEAD_STATUSES))
async def test_pulse_never_calls_a_dead_account_healthy(status):
    """Падало на `spamblock`: пульс показывал спам-блок «здоровым»."""
    out = await infra_memory.get_account_health(_HealthPool([status]), 777)
    assert out["summary"] == {"healthy": 0, "at_risk": 0,
                              "quarantine": 1, "total": 1}, status
    assert out["accounts"][0]["status"] == "quarantine"


async def test_pulse_still_calls_a_working_account_healthy():
    """Обратная сторона: сведение не должно хоронить живых."""
    out = await infra_memory.get_account_health(
        _HealthPool(["active", "ok", "cooldown"]), 777)
    assert out["summary"]["healthy"] == 3
    assert out["summary"]["quarantine"] == 0


# ── Экран флота ────────────────────────────────────────────────────────────

class _FleetPool:
    def __init__(self, statuses: list[str]):
        self.statuses = statuses

    async def fetch(self, sql, *args):
        if "account_rehab_state" in sql:
            return []
        return [
            {"id": 1000 + i, "phone": f"+7000000{i:04d}", "first_name": "A",
             "username": None, "acc_status": s, "cooldown_until": None,
             "trust_score": 0.5}
            for i, s in enumerate(self.statuses)
        ]


@pytest.mark.parametrize("status", sorted(acc_status.DEAD_STATUSES))
async def test_fleet_screen_calls_the_same_statuses_dead(status):
    """Падало на `deleted`/`frozen`: экран держал их «готовыми к действиям»."""
    states = await fleet_pulse.account_states(_FleetPool([status]), 777)
    assert states[0]["state"] == "dead", status


@pytest.mark.parametrize("status", sorted(acc_status.DEAD_STATUSES))
def test_dead_reason_is_in_russian(status):
    """Владелец не читает по-английски: причина не может быть сырым статусом."""
    reason = fleet_pulse._dead_reason(status)
    assert reason != status, f"нет человеческой причины для {status}"
    assert any("а" <= ch.lower() <= "я" for ch in reason), reason


# ── Храповик: второго списка не заводить ───────────────────────────────────

_ORGANS = (infra_memory, fleet_pulse, resource_selector, immunity_engine)


def _hand_written_sets(src: str) -> list[str]:
    """Литеральные наборы/сравнения `in (...)` со мёртвыми статусами внутри.

    Разбор по AST, не по окну строк: собственный список переживает любое
    переформатирование, а храповик с фиксированным окном выключается от
    лишнего комментария (`test_no_silently_disabled_guards`).

    Словари (`_dead_reason`) не считаются: там статус — КЛЮЧ человеческой
    причины, это не второй источник правды, а перевод на русский.
    """
    tree = ast.parse(src)
    found: list[str] = []

    def members(node) -> set[str]:
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            return {e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        return set()

    for node in ast.walk(tree):
        bags = []
        if isinstance(node, ast.Compare):
            bags = [members(c) for c in node.comparators]
        elif isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            bags = [members(node)]
        for bag in bags:
            if len(bag & acc_status.DEAD_STATUSES) >= 2:
                found.append(", ".join(sorted(bag & acc_status.DEAD_STATUSES)))
    return found


@pytest.mark.parametrize("module", _ORGANS, ids=lambda m: m.__name__)
def test_organ_does_not_keep_its_own_dead_list(module):
    own = _hand_written_sets(inspect.getsource(module))
    assert not own, (
        f"{module.__name__} держит свой набор мёртвых статусов ({own}) — "
        "именно так `spamblock` однажды разъехался между пульсом и выбором; "
        "читайте account_status.is_dead/DEAD_STATUSES")


@pytest.mark.parametrize("module", _ORGANS, ids=lambda m: m.__name__)
def test_organ_does_not_hard_code_the_list_in_sql(module):
    """Тот же список, но вписанный в текст запроса, — та же беда."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "acc_status" in text:
                hits = {s for s in acc_status.DEAD_STATUSES if f"'{s}'" in text}
                assert len(hits) < 2, (
                    f"{module.__name__}: набор мёртвых статусов вписан в SQL "
                    f"({sorted(hits)}) — подставляйте account_status.sql_dead_list()")


def test_the_probe_itself_catches_a_hand_written_list():
    """Измеритель проверяем на заведомо больном примере, а не только на коде.

    Пробник, который молчит на всём, читается как «чисто» и ничего не стережёт.
    """
    sick = ("def pick(status):\n"
            "    return status not in ('banned', 'session_expired')\n")
    assert _hand_written_sets(sick), "пробник не видит даже выписанный вручную список"

    healthy = ("from services import account_status\n"
               "def pick(status):\n"
               "    return not account_status.is_dead(status)\n")
    assert not _hand_written_sets(healthy), "пробник ругается на правильный код"


# ── Живая БД: четыре органа об одном аккаунте ──────────────────────────────

@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
async def test_pulse_fleet_and_selection_agree_on_a_real_account():
    """Один спам-блокнутый аккаунт в живой схеме — три органа, один ответ.

    Это и есть то, чего не было: заглушки согласие не ловят, потому что у
    каждого органа своя заглушка. Здесь все три читают одну строку.
    """
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    owner = 918273645
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            dead_id = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, acc_status, is_active) "
                "VALUES ($1, $2, 'x', 'spamblock', TRUE) RETURNING id",
                owner, "+70000000001")
            live_id = await conn.fetchval(
                "INSERT INTO tg_accounts (owner_id, phone, session_str, acc_status, is_active) "
                "VALUES ($1, $2, 'x', 'active', TRUE) RETURNING id",
                owner, "+70000000002")

        pulse = await infra_memory.get_account_health(pool, owner)
        by_id = {a["account_id"]: a for a in pulse["accounts"]}
        assert by_id[dead_id]["status"] == "quarantine"
        assert by_id[live_id]["status"] == "healthy"

        fleet = {s["id"]: s for s in
                 await fleet_pulse.account_states(pool, owner)}
        assert fleet[dead_id]["state"] == "dead"
        assert fleet[live_id]["state"] == "ready"

        picked = await resource_selector.select_all_active(pool, owner)
        picked_ids = {int(r["id"]) for r in picked}
        assert dead_id not in picked_ids
        assert live_id in picked_ids
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
        await pool.close()
