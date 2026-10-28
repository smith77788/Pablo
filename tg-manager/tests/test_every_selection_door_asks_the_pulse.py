"""Все двери выбора аккаунта спрашивают риск-пульс — не только одна из трёх.

ЧТО ЛОМАЛОСЬ. Карантин (`restriction_events` + `risk_cleared_at`) проверялся
только там, где исполнитель не забыл позвать `_filter_quarantined_accounts`.
Сами двери выбора его не знали: `flood_engine.get_best_account` и
`get_active_accounts` фильтруют мёртвые статусы и кулдаун, а подтверждённое
свежее ограничение не спрашивали вовсе — аккаунт, который риск-пульс только что
убрал из работы, спокойно выигрывал ранжирование и шёл в следующую операцию.
Через `get_best_account` аккаунт берёт `resource_selector.select_accounts`, то
есть и волны strike, и «оживление» сообществ.

Правило «в карантине ВСЕ — работаем всеми» обязано совпадать во всех трёх
местах (`op_worker._filter_quarantined_accounts`, `select_all_active`, здесь):
разъехавшись, они дали бы разное поведение на одном и том же флоте.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from services import flood_engine as fe


class _Pool:
    def __init__(self, ids, quarantined=(), boom=False):
        self._ids = list(ids)
        self._quarantined = set(quarantined)
        self._boom = boom
        self.quar_calls = 0

    async def fetch(self, q, *a):
        if "restriction_events" in q:
            self.quar_calls += 1
            if self._boom:
                raise RuntimeError("пульс недоступен")
            asked = a[0] if a else []
            return [{"account_id": i} for i in asked if i in self._quarantined]
        return [self._row(i) for i in self._ids]

    async def execute(self, q, *a):
        return "UPDATE 1"

    @staticmethod
    def _row(i):
        return {
            "id": i, "owner_id": 1, "tg_user_id": None, "session_str": "sess",
            "first_name": None, "phone": f"+7900000{i:04d}", "device_model": None,
            "system_version": None, "app_version": None, "lang_code": None,
            "system_lang_code": None, "proxy_id": None, "cf_relay_url": None,
            "trust_score": 1.0, "cooldown_until": None, "tags": None, "pool": None,
            "last_used": None, "is_premium": None, "proxy_url": None,
            "geo_country": None, "physics_ban_probability": 0.0,
        }


@pytest.fixture(autouse=True)
def _clean_memory():
    """Процессная память флуда не должна протекать между проверками."""
    fe._flood_state.clear()
    yield
    fe._flood_state.clear()


async def test_best_account_skips_the_quarantined_one():
    acc = await fe.get_best_account(_Pool([1, 2], quarantined=[1]), 1)
    assert acc is not None and int(acc["id"]) == 2, (
        "дверь отдала аккаунт, который риск-пульс только что убрал из работы"
    )


async def test_best_account_asks_the_pulse():
    pool = _Pool([1, 2])
    await fe.get_best_account(pool, 1)
    assert pool.quar_calls == 1, "дверь вообще не спрашивает риск-пульс"


async def test_best_account_works_when_the_whole_fleet_is_quarantined():
    """То же правило, что у двух других дверей: обнулить операцию хуже."""
    acc = await fe.get_best_account(_Pool([1, 2], quarantined=[1, 2]), 1)
    assert acc is not None, (
        "на полностью карантинном флоте дверь вернула пусто — это расходится "
        "с op_worker._filter_quarantined_accounts и select_all_active"
    )


async def test_best_account_fails_open():
    acc = await fe.get_best_account(_Pool([1], quarantined=[1], boom=True), 1)
    assert acc is not None, "сбой пульса не должен останавливать ядро"


async def test_active_accounts_skip_the_quarantined_one():
    rows = await fe.get_active_accounts(_Pool([1, 2, 3], quarantined=[2]), 1)
    assert sorted(int(r["id"]) for r in rows) == [1, 3]


async def test_active_accounts_survive_a_wholly_quarantined_fleet():
    rows = await fe.get_active_accounts(_Pool([1, 2], quarantined=[1, 2]), 1)
    assert sorted(int(r["id"]) for r in rows) == [1, 2]


async def test_active_accounts_fail_open():
    rows = await fe.get_active_accounts(_Pool([1, 2], quarantined=[1], boom=True), 1)
    assert sorted(int(r["id"]) for r in rows) == [1, 2]


def _calls(fn):
    """Границы функции — по ast, а не окном фиксированной длины."""
    tree = ast.parse(inspect.getsource(fe))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn
    )
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


@pytest.mark.parametrize("fn", ["get_best_account", "get_active_accounts"])
def test_the_door_keeps_the_gate(fn):
    assert "_drop_quarantined" in _calls(fn), (
        f"{fn} перестала спрашивать риск-пульс — исполнители без своего "
        f"фильтра снова получат флагнутые аккаунты"
    )
