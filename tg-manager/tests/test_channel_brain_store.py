"""Channel Brain Store — персистентная политика канала (va_channel_brain, schema_v222).

Проверяем разбор строки БД, capability поверх политики, upsert-возврат, fail-soft
и что миграция зарегистрирована в манифесте контрольных сумм.
"""
from __future__ import annotations

import os

import pytest

from services import channel_brain as cb
from services import channel_brain_store as st
from tests.test_executors import FakePool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row(**over):
    base = {
        "owner_id": 42,
        "channel_key": "@brand",
        "brand_rules": '{"max_emoji": 2, "max_cta": 1, "forbidden_words": ["бесплатно"]}',
        "pillars": ["ad", "story", "case"],
        "mix_weights": {"ad": 1.0, "story": 1.0, "case": 1.0},
        "autonomy_mode": "semi",
        "max_streak": 2,
        "dup_threshold": 0.6,
    }
    base.update(over)
    return base


def test_as_obj_parses_str_and_passthrough():
    assert st._as_obj('{"a": 1}', {}) == {"a": 1}
    assert st._as_obj(["x"], []) == ["x"]
    assert st._as_obj(None, "def") == "def"
    assert st._as_obj("not-json", {"d": 1}) == {"d": 1}  # мусор → default


def test_brand_rules_from_dict_lists_become_tuples():
    r = st.brand_rules_from_dict({"max_emoji": 3, "forbidden_words": ["a", "b"]})
    assert isinstance(r, cb.BrandRules)
    assert r.max_emoji == 3
    assert r.forbidden_words == ("a", "b")


@pytest.mark.asyncio
async def test_get_profile_parses_and_capability_works():
    pool = FakePool(fetchrow=_row())
    brain = await st.get_profile(pool, 42, "@brand")
    assert brain is not None
    assert brain.channel_key == "@brand" and brain.autonomy_mode == "semi"
    assert brain.brand_rules.max_emoji == 2
    # capability: черновик с 3 эмодзи и запрещённым словом → на ревью
    v = brain.check_draft("Только сегодня бесплатно 🔥🔥🔥", recent_texts=[])
    assert v.needs_review is True
    # план рубрик: 'case' ни разу не выходил → берём его
    assert brain.plan_next(["ad", "story", "ad", "story"]) == "case"


@pytest.mark.asyncio
async def test_get_profile_none_when_absent():
    pool = FakePool(fetchrow=None)
    assert await st.get_profile(pool, 42, "@nope") is None


@pytest.mark.asyncio
async def test_save_profile_returns_id():
    pool = FakePool(fetchrow={"id": 7})
    new_id = await st.save_profile(
        pool, 42, "@brand",
        brand_rules={"max_cta": 1}, pillars=["ad"], autonomy_mode="autonomous",
    )
    assert new_id == 7


@pytest.mark.asyncio
async def test_get_profile_failsoft_on_db_error():
    class Boom(FakePool):
        async def fetchrow(self, *a, **k):
            raise RuntimeError("db down")
    assert await st.get_profile(Boom(), 42, "@brand") is None


@pytest.mark.asyncio
async def test_save_profile_failsoft_on_db_error():
    class Boom(FakePool):
        async def fetchrow(self, *a, **k):
            raise RuntimeError("db down")
    assert await st.save_profile(Boom(), 42, "@brand") is None


def test_migration_registered_in_checksums():
    fname = "schema_v222_va_channel_brain.sql"
    assert os.path.exists(os.path.join(ROOT, fname)), "миграция отсутствует"
    manifest = open(os.path.join(ROOT, "schema_checksums.txt"), encoding="utf-8").read()
    assert fname in manifest, "миграция не внесена в schema_checksums.txt"
