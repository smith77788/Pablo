"""Разбор инвайтинга не опрашивает флот по одному аккаунту.

ЧТО БЫЛО СЛОМАНО (производительность). На каждый аккаунт делались ДВЕ
проверки — карантин риск-пульса и остаток суточного лимита. Раньше их пустили
параллельно с потолком в 8, но число запросов от этого не менялось: на стенде с
2500 рабочими аккаунтами одно открытие «Разбора инвайта» стоило 5003 запросов
и ~1 с, а восемь соединений пула были заняты всё это время. Теперь обе
проверки пакетные (`infra_memory.quarantined_accounts`,
`flood_engine.recommended_daily_limits`), и число запросов не зависит от
размера флота.

Пакетный лимит обязан давать ТО ЖЕ, что поштучный, — проверяем на одних и тех
же строках.
"""
from __future__ import annotations

import asyncio

from services import flood_engine as fe
from services import invite_advisor as adv


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _acc(i):
    return {"id": i, "label": f"+7999{i:07d}", "first_name": "Иван", "username": f"u{i}",
            "has_photo": True, "warmup_level": "medium"}


class _CountingPool:
    def __init__(self, n):
        self.accounts = [_acc(i) for i in range(1, n + 1)]
        self.calls = 0

    async def fetch(self, q, *a):
        self.calls += 1
        if "account_daily_stats" in q:
            return []
        if "restriction_events" in q:
            return []
        if "tg_accounts" in q:
            return self.accounts
        return []

    async def fetchrow(self, q, *a):
        self.calls += 1
        return None

    async def fetchval(self, q, *a):
        self.calls += 1
        return None


def test_query_count_does_not_grow_with_the_fleet():
    small, big = _CountingPool(3), _CountingPool(300)
    _run(adv.build_advice(small, 100))
    _run(adv.build_advice(big, 100))
    assert big.calls == small.calls, (
        f"разбор делает запрос на аккаунт: {small.calls} на 3 аккаунта, "
        f"{big.calls} на 300")


# ── пакетный лимит = поштучный ───────────────────────────────────────────────

_STATS = {
    1: {"account_id": 1, "inv_ok": 40, "fails": 2, "floods": 0, "active_days": 5,
        "best_day": 18, "today": 4},
    2: {"account_id": 2, "inv_ok": 10, "fails": 30, "floods": 2, "active_days": 3,
        "best_day": 9, "today": 9},
}
_FACTORS = {
    1: {"id": 1, "first_name": "", "username": "a", "warmup_level": None, "tg_user_id": 1,
        "is_premium": None, "has_photo": None, "profile_checked_at": None,
        "phone": "+79990000001", "lang_code": "ru", "system_lang_code": "ru-RU",
        "reg_date": None},
    2: {"id": 2, "first_name": "Пётр", "username": "p", "warmup_level": "deep",
        "tg_user_id": 2, "is_premium": None, "has_photo": None, "profile_checked_at": None,
        "phone": "+79990000002", "lang_code": "en", "system_lang_code": "en-US",
        "reg_date": None},
}


class _StatsPool:
    """Отвечает и поштучным, и пакетным запросам из одних и тех же строк."""

    async def fetchrow(self, q, *a):
        acc = int(a[0])
        if "account_daily_stats" in q:
            return _STATS.get(acc)
        return _FACTORS.get(acc)

    async def fetch(self, q, *a):
        ids = [int(x) for x in a[0]]
        src = _STATS if "account_daily_stats" in q else _FACTORS
        return [src[i] for i in ids if i in src]


def test_batch_limits_match_one_by_one():
    pool = _StatsPool()
    batch = _run(fe.recommended_daily_limits(pool, [1, 2, 3]))
    for acc in (1, 2, 3):
        one = _run(fe.recommended_daily_limit(pool, acc))
        assert batch[acc] == one, (acc, batch[acc], one)


def test_batch_limits_fail_to_cold_start():
    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db down")

    res = _run(fe.recommended_daily_limits(Boom(), [5, 6]))
    assert set(res) == {5, 6}
    assert all(r["remaining"] == fe._INVITE_LIMIT_COLD_START for r in res.values())
