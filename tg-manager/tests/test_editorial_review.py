"""Editorial Review — редакционный гейт на реальном пути (политика + история + gate).

Проверяем: массовый режим (правила по умолчанию + история владельца), режим канала
(политика va_channel_brain), fail-soft, форматирование подсказки и что предпросмотр
массовой публикации действительно зовёт review_draft.
"""
from __future__ import annotations

import os

import pytest

from services import channel_brain as cb
from services import editorial_review as er
from tests.test_executors import FakePool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.asyncio
async def test_review_owner_mode_flags_near_duplicate():
    # channel_key=None → recent_texts_for_owner (fetch) + правила по умолчанию.
    recent = "Купите наш новый курс сегодня со скидкой пятьдесят процентов"
    pool = FakePool(fetch=[{"body": recent}])
    verdict = await er.review_draft(pool, 42, recent)  # тот же текст = повтор
    assert verdict.needs_review is True
    assert any("повтор" in r.lower() or "вступление" in r.lower() for r in verdict.reasons)


@pytest.mark.asyncio
async def test_review_owner_mode_clean_draft_ok():
    pool = FakePool(fetch=[{"body": "Совсем другой пост про погоду и котиков"}])
    verdict = await er.review_draft(pool, 42, "Свежий анонс вебинара по налогам во вторник")
    assert verdict.needs_review is False
    assert verdict.ok is True


@pytest.mark.asyncio
async def test_review_channel_mode_applies_brand_rules():
    # get_profile (fetchrow) отдаёт политику с лимитом эмодзи=1; recent (fetch)=[].
    profile = {
        "owner_id": 42,
        "channel_key": "@brand",
        "brand_rules": '{"max_emoji": 1}',
        "pillars": [],
        "mix_weights": {},
        "autonomy_mode": "manual",
        "max_streak": 2,
        "dup_threshold": 0.6,
    }
    pool = FakePool(fetchrow=profile, fetch=[])
    verdict = await er.review_draft(pool, 42, "Огонь 🔥🔥🔥", channel_key="@brand")
    assert verdict.needs_review is True
    assert any("эмодзи" in r for r in verdict.reasons)


@pytest.mark.asyncio
async def test_review_failsoft_returns_ok_on_error():
    class Boom(FakePool):
        async def fetch(self, *a, **k):
            raise RuntimeError("db down")
    verdict = await er.review_draft(Boom(), 42, "любой текст")
    assert verdict.ok is True and verdict.needs_review is False


def test_format_advisory_empty_when_ok():
    assert er.format_advisory(cb.EditorialVerdict(ok=True, needs_review=False)) == ""


def test_format_advisory_lists_reasons_and_says_not_blocking():
    v = cb.EditorialVerdict(ok=False, needs_review=True, reasons=["почти-повтор недавнего поста (похожесть 0.9)"])
    out = er.format_advisory(v)
    assert "Редактор советует" in out
    assert "почти-повтор" in out
    assert "можно запустить как есть" in out


def test_mass_publish_preview_calls_editorial_review():
    """Предпросмотр ДОЛЖЕН звать редакционный гейт — иначе подключение снова фикция."""
    src = open(
        os.path.join(ROOT, "bot", "handlers", "mass_publish.py"), encoding="utf-8"
    ).read()
    assert "editorial_review.review_draft(" in src, "предпросмотр не зовёт review_draft"
    assert "editorial_review.format_advisory(" in src, "подсказка редактора не выводится"
