"""Карантин риск-пульса стоит в ДВЕРИ выбора, а не в памяти каждого исполнителя.

ЧТО ЛОМАЛОСЬ. Мёртвые статусы и кулдаун в `select_all_active` уже переехали, а
карантин остался «ручным»: исполнитель должен был сам позвать
`_filter_quarantined_accounts` ПОСЛЕ выбора. Двадцать восемь позвали, остальные
нет — и дверь честно отдавала им аккаунт с подтверждённым свежим ограничением.
Среди забывших был `phone_check`, в собственном комментарии которого написано
«ImportContacts — одна из самых баноопасных операций Telegram»: аккаунт он берёт
через `select_account_rotated`, то есть через эту же дверь. Защита, которую надо
не забыть позвать, — это не защита, а соглашение.

Отдельно проверяем правило «в карантине ВСЕ — работаем всеми»: оно скопировано у
`_filter_quarantined_accounts` дословно, и разъехаться эти два места не должны.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from services import resource_selector as rsel


class _Pool:
    """Заглушка: отдаёт заданный флот и заданный список карантинных id."""

    def __init__(self, ids, quarantined=(), quar_boom=False):
        self._ids = list(ids)
        self._quarantined = set(quarantined)
        self._quar_boom = quar_boom
        self.quar_calls = 0

    async def fetch(self, q, *a):
        if "restriction_events" in q:
            self.quar_calls += 1
            if self._quar_boom:
                raise RuntimeError("пульс недоступен")
            asked = a[0] if a else []
            return [{"account_id": i} for i in asked if i in self._quarantined]
        return [self._row(i) for i in self._ids]

    @staticmethod
    def _row(i):
        return {
            "id": i, "owner_id": 1, "tg_user_id": None, "phone": f"+7900000{i:04d}",
            "first_name": None, "username": None, "session_str": "sess",
            "is_active": True, "device_model": None, "system_version": None,
            "app_version": None, "lang_code": None, "system_lang_code": None,
            "proxy_id": None, "cf_relay_url": None, "tags": None, "pool": None,
            "labels": None, "warnings": None, "project": None, "trust_score": 1.0,
            "cooldown_until": None, "is_premium": None, "acc_status": "active",
            "proxy_url": None, "geo_country": None, "proxy_alive": True,
            "proxy_fail_streak": 0,
        }


def _ids(rows):
    return sorted(int(r["id"]) for r in rows)


async def test_the_door_drops_quarantined_accounts():
    pool = _Pool([1, 2, 3], quarantined=[2])
    rows = await rsel.select_all_active(pool, 1, min_trust_score=0.0)
    assert _ids(rows) == [1, 3], (
        "дверь отдала аккаунт с подтверждённым свежим ограничением — "
        "действие с него это быстрый бан"
    )


async def test_the_door_asks_the_pulse_at_all():
    pool = _Pool([1, 2, 3], quarantined=[])
    await rsel.select_all_active(pool, 1, min_trust_score=0.0)
    assert pool.quar_calls == 1, "дверь вообще не спрашивает риск-пульс"


async def test_a_clean_fleet_passes_whole():
    pool = _Pool([1, 2, 3], quarantined=[])
    rows = await rsel.select_all_active(pool, 1, min_trust_score=0.0)
    assert _ids(rows) == [1, 2, 3]


async def test_a_wholly_quarantined_fleet_still_works():
    """Правило взято у _filter_quarantined_accounts: обнулить операцию хуже."""
    pool = _Pool([1, 2], quarantined=[1, 2])
    rows = await rsel.select_all_active(pool, 1, min_trust_score=0.0)
    assert _ids(rows) == [1, 2], (
        "дверь обнулила операцию на полностью карантинном флоте — у двадцати "
        "восьми уже закрытых исполнителей это молча сменило бы поведение"
    )


async def test_the_gate_fails_open():
    pool = _Pool([1, 2], quarantined=[1], quar_boom=True)
    rows = await rsel.select_all_active(pool, 1, min_trust_score=0.0)
    assert _ids(rows) == [1, 2], "сбой пульса не должен останавливать ядро"


async def test_loading_a_pool_may_see_quarantined_accounts():
    """Загрузка пула — не действие: отбор делается при раздаче."""
    pool = _Pool([1, 2], quarantined=[2])
    rows = await rsel.select_all_active(
        pool, 1, min_trust_score=0.0, respect_quarantine=False)
    assert _ids(rows) == [1, 2]
    assert pool.quar_calls == 0, "при выключенном гейте лишний запрос не нужен"


async def test_the_rotated_pick_inherits_the_gate():
    """phone_check берёт аккаунт отсюда — через ту же дверь."""
    pool = _Pool([1, 2], quarantined=[1])
    acc = await rsel.select_account_rotated(pool, 1, action_type="default")
    assert acc is not None and int(acc["id"]) == 2, (
        "одиночный выбор отдал карантинный аккаунт под ImportContacts"
    )


def test_only_a_loader_may_opt_out():
    """Выключать гейт вправе только загрузка пула — больше никто."""
    import pathlib

    allowed = {"services/session_pool.py"}
    offenders = []
    root = pathlib.Path(".")
    for path in list(root.glob("services/**/*.py")) + list(root.glob("bot/**/*.py")):
        rel = path.as_posix()
        if rel in allowed or rel.endswith("resource_selector.py"):
            continue
        if "respect_quarantine=False" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        "гейт карантина выключен вне загрузки пула:\n  " + "\n  ".join(offenders)
        + "\n\nВыключать его вправе только путь, который НЕ действует аккаунтом."
    )


def test_the_door_keeps_the_gate_in_source():
    """Храповик: условие могли бы выкинуть при следующей правке двери."""
    src = inspect.getsource(rsel.select_all_active)
    tree = ast.parse(inspect.getsource(rsel))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "select_all_active"
    )
    calls = {
        n.func.attr for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "quarantined_accounts" in calls, (
        "дверь выбора перестала спрашивать риск-пульс — все исполнители, "
        "у которых нет своего фильтра, снова получат флагнутые аккаунты"
    )
    assert "respect_quarantine" in src
