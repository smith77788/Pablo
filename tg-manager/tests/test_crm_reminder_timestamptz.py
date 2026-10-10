"""CRM upsert: строка-дата в TIMESTAMPTZ роняла запрос (класс #15).

uch_crm_upsert передаёт СЫРОЕ тело запроса в upsert_crm, а тот биндил
next_reminder_at/last_interaction_at (TIMESTAMPTZ) как $N без ::timestamptz.
asyncpg на строку кидает DataError → сохранение CRM с датой падало 500 всегда.
Фикс: нормализация timestamptz-полей в datetime на границе сервиса.
"""
from __future__ import annotations

import datetime as dt

import pytest

from services.contacts_hub import crm_engine
from services.contacts_hub.crm_engine import _coerce_dt, upsert_crm


class RecPool:
    """Фейк-пул: fetchrow → заданный existing; execute запоминает биндинги."""
    def __init__(self, existing=None):
        self._existing = existing
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        return self._existing

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"


# ── _coerce_dt ────────────────────────────────────────────────────────────────

def test_coerce_none_and_datetime_passthrough():
    assert _coerce_dt(None) is None
    d = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert _coerce_dt(d) is d


def test_coerce_iso_string_with_z():
    got = _coerce_dt("2026-07-25T15:00:00Z")
    assert isinstance(got, dt.datetime)
    assert got.tzinfo is not None
    assert got == dt.datetime(2026, 7, 25, 15, 0, tzinfo=dt.timezone.utc)


def test_coerce_naive_string_treated_as_utc():
    got = _coerce_dt("2026-07-25T15:00:00")
    assert got.tzinfo == dt.timezone.utc


def test_coerce_empty_string_is_none():
    assert _coerce_dt("") is None
    assert _coerce_dt("   ") is None


def test_coerce_bad_string_raises():
    with pytest.raises(ValueError):
        _coerce_dt("не-дата")


# ── upsert_crm: строка не должна долетать до asyncpg сырой ────────────────────

@pytest.mark.asyncio
async def test_insert_branch_binds_datetime_not_string():
    pool = RecPool(existing=None)  # нет записи → INSERT
    await upsert_crm(pool, 1, "c1", {"next_reminder_at": "2026-07-25T15:00:00Z"})
    # найти INSERT-вызов и проверить, что среди аргументов есть datetime, не str
    ins = [c for c in pool.execute_calls if "INSERT INTO contact_crm" in c[0]]
    assert ins, "ожидался INSERT"
    args = ins[0][1]
    assert any(isinstance(a, dt.datetime) for a in args)
    assert not any(isinstance(a, str) and "2026-07-25T15:00:00" in a for a in args), \
        "сырая строка-дата долетела до биндинга"


@pytest.mark.asyncio
async def test_update_branch_binds_datetime_not_string():
    pool = RecPool(existing={"stage": "lead", "custom_fields": "{}"})  # есть → UPDATE
    await upsert_crm(pool, 1, "c1", {"next_reminder_at": "2026-07-25T15:00:00Z"})
    upd = [c for c in pool.execute_calls if c[0].strip().startswith("UPDATE contact_crm")]
    assert upd, "ожидался UPDATE"
    args = upd[0][1]
    assert any(isinstance(a, dt.datetime) for a in args)


@pytest.mark.asyncio
async def test_bad_date_raises_valueerror():
    pool = RecPool(existing=None)
    with pytest.raises(ValueError):
        await upsert_crm(pool, 1, "c1", {"next_reminder_at": "totally-bad"})


@pytest.mark.asyncio
async def test_no_ts_fields_unchanged():
    pool = RecPool(existing=None)
    await upsert_crm(pool, 1, "c1", {"stage": "won"})  # без дат — не падает
    assert pool.execute_calls
