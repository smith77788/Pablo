"""Круг каналов аккаунта растёт сам — иначе весь флот ходит по одним адресам.

Пул прогрева — двадцать один @канал, зашитый в код, и по восемь на аккаунт,
навсегда. Это уже второй раз тот же дефект: совпадающий граф вступлений мы
разводили сидом от account_id, но САМ СПИСОК остался общим, и через восемь
каналов на аккаунт когорта всё равно смотрится одинаково.

Здесь проверяется механизм, который это чинит: найденное аккаунтом (похожие
каналы, поиск, ссылки из постов) складывается в его личный список и попадает в
оборот, а круг расширяется по мере прогрева.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import account_warmer as aw  # noqa: E402


class _Pool:
    """Пул, который ведёт себя как таблица интересов: запоминает вставленное."""

    def __init__(self, stored=None, niche=None, fail=False):
        self.rows: list[dict] = list(stored or [])
        self.niche = niche
        self.fail = fail
        self.deletes = 0

    async def executemany(self, sql, args):
        if self.fail:
            raise RuntimeError("база недоступна")
        assert "account_warmup_interests" in sql
        for acc, ref, source in args:
            for r in self.rows:
                if r["account_id"] == acc and r["channel_ref"] == ref:
                    r["seen_count"] += 1
                    break
            else:
                self.rows.append(
                    {"account_id": acc, "channel_ref": ref, "source": source, "seen_count": 0}
                )

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("база недоступна")
        if sql.lstrip().upper().startswith("DELETE"):
            self.deletes += 1

    async def fetch(self, sql, *args):
        if self.fail:
            raise RuntimeError("база недоступна")
        acc = args[0]
        return [r for r in self.rows if r["account_id"] == acc]

    async def fetchrow(self, sql, *args):
        return self.niche


def _run(coro):
    return asyncio.run(coro)


# ── круг каналов ─────────────────────────────────────────────────────────────


def test_circle_grows_with_warmup_day():
    """Живой человек за месяц подписан на большее, чем за первую неделю."""
    assert aw.channel_pool_size(0) < aw.channel_pool_size(10) < aw.channel_pool_size(25)


def test_circle_has_a_ceiling():
    """Без потолка на тридцатый день аккаунт держал бы в обороте всё подряд."""
    assert aw.channel_pool_size(10_000) <= 28


def test_circle_survives_garbage_day():
    assert aw.channel_pool_size(None) >= 8
    assert aw.channel_pool_size("день") >= 8


def test_circle_starts_no_narrower_than_before():
    """Регрессия: раньше было ровно 8, сузить нельзя."""
    assert aw.channel_pool_size(0) >= 8


# ── находки попадают в базу ──────────────────────────────────────────────────


def test_found_channels_are_remembered():
    pool = _Pool()
    assert _run(aw.remember_interests(pool, 5, ["@a", "@b"])) == 2
    assert {r["channel_ref"] for r in pool.rows} == {"@a", "@b"}


def test_repeated_find_is_not_a_duplicate():
    pool = _Pool()
    _run(aw.remember_interests(pool, 5, ["@a"]))
    _run(aw.remember_interests(pool, 5, ["@a"]))
    assert len(pool.rows) == 1
    assert pool.rows[0]["seen_count"] == 1


def test_duplicates_inside_one_run_collapse():
    pool = _Pool()
    assert _run(aw.remember_interests(pool, 5, ["@a", "@a", "@a"])) == 1


def test_nothing_found_touches_nothing():
    pool = _Pool()
    assert _run(aw.remember_interests(pool, 5, [])) == 0
    assert pool.rows == [] and pool.deletes == 0


def test_list_is_capped():
    """Поиск и «похожие каналы» приносят десятки ссылок в день; без потолка
    список рос бы бесконечно."""
    pool = _Pool()
    _run(aw.remember_interests(pool, 5, [f"@c{i}" for i in range(200)]))
    assert len(pool.rows) <= aw._INTEREST_LIMIT
    assert pool.deletes == 1, "вытеснение старых находок не выполняется"


def test_broken_database_does_not_break_warmup():
    """Журнал интересов — украшение прогрева, а не его условие."""
    assert _run(aw.remember_interests(_Pool(fail=True), 5, ["@a"])) == 0
    assert _run(aw.get_discovered_channels(_Pool(fail=True), 5)) == []


# ── находки попадают В ОБОРОТ ────────────────────────────────────────────────


def test_discovered_channels_enter_the_rotation():
    """Главное: найденное должно РАБОТАТЬ, а не лежать в базе.

    Если сложить находки в хвост за двадцатью зашитыми каналами, они никогда не
    попадут в оборот — это была бы таблица ради таблицы.
    """
    pool = _Pool(stored=[{"account_id": 7, "channel_ref": "@found", "seen_count": 0}])
    circle = _run(aw.warmup_channel_pool(pool, 7, warmup_day=5))
    assert "@found" in circle


def test_rotation_keeps_the_base_channels_too():
    pool = _Pool(stored=[{"account_id": 7, "channel_ref": "@found", "seen_count": 0}])
    circle = _run(aw.warmup_channel_pool(pool, 7, warmup_day=5))
    assert len(circle) > 1, "найденное вытеснило базовый пул целиком"
    assert any(c in aw._WARMUP_PUBLIC_CHANNELS for c in circle)


def test_rotation_has_no_duplicates():
    pool = _Pool(
        stored=[{"account_id": 7, "channel_ref": c, "seen_count": 0}
                for c in aw._WARMUP_PUBLIC_CHANNELS[:5]]
    )
    circle = _run(aw.warmup_channel_pool(pool, 7, warmup_day=20))
    assert len(circle) == len(set(circle))


def test_rotation_respects_the_circle_size():
    pool = _Pool(
        stored=[{"account_id": 7, "channel_ref": f"@f{i}", "seen_count": 0} for i in range(50)]
    )
    circle = _run(aw.warmup_channel_pool(pool, 7, warmup_day=3))
    assert len(circle) <= aw.channel_pool_size(3)


def test_accounts_do_not_share_a_circle():
    """Ради этого всё и делается."""
    pool = _Pool()
    a = _run(aw.warmup_channel_pool(pool, 101, warmup_day=5))
    b = _run(aw.warmup_channel_pool(pool, 202, warmup_day=5))
    assert a != b


def test_circle_is_stable_for_one_account():
    """Человек не перевыбирает интересы каждый день."""
    pool = _Pool()
    first = _run(aw.warmup_channel_pool(pool, 101, warmup_day=5))
    assert _run(aw.warmup_channel_pool(pool, 101, warmup_day=5)) == first


# ── проводка в оба пути прогрева ─────────────────────────────────────────────


def _src(rel: str) -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_both_paths_save_what_they_found():
    src = _src("services/account_warmer.py")
    assert src.count("await remember_interests(") >= 2, (
        "один из путей прогрева выбрасывает найденные каналы"
    )


def test_both_paths_use_the_widened_circle():
    src = _src("services/account_warmer.py")
    assert "await warmup_channel_pool(" in src
    assert "await get_discovered_channels(" in src


def test_discovering_actions_are_marked_as_such():
    discovering = [n for n, s in aw._ACTION_SPECS.items() if s.discovers]
    assert set(discovering) >= {"explore_similar", "search", "open_link"}


def test_owner_can_see_the_circle():
    """Невидимая механика — недоделанная механика: подтвердить, что круг растёт
    (или заметить, что он встал), владельцу должно быть чем."""
    api = _src("services/mini_app_api.py")
    ui = _src("mini_app/index.html")
    assert "account_warmup_interests" in api, "эндпойнт журнала не отдаёт круг каналов"
    assert '"circle"' in api
    assert "d.circle" in ui, "мини-апп не показывает круг каналов"
    assert "Круг каналов" in ui


def test_migration_exists():
    """Таблица должна появиться в проде, а не только в коде."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    sql = list(root.glob("schema_v*_warmup_interests.sql"))
    assert sql, "нет миграции для account_warmup_interests"
    text = sql[0].read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS account_warmup_interests" in text
    assert "PRIMARY KEY (account_id, channel_ref)" in text
