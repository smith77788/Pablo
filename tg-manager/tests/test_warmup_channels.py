"""Регрессия: разбор пользовательских каналов для прогрева.

Warmup UI теперь принимает свои каналы (раньше account_niche_profiles никто не
заполнял — весь профиль/ниша/каналы были мёртвым кодом). Нормализация должна
принимать @name, t.me/name, список/строку; отсекать мусор; дедуплицировать;
ограничивать количество.
"""
from __future__ import annotations

from services.account_warmer import (
    normalize_warmup_channels,
    WARMUP_PROFILES,
    WARMUP_NICHES,
)


def test_string_newline_and_comma_separated():
    raw = "@alpha\n@beta, gamma\nt.me/delta"
    assert normalize_warmup_channels(raw) == ["@alpha", "@beta", "@gamma", "@delta"]


def test_strips_at_and_tme_prefixes():
    assert normalize_warmup_channels(["https://t.me/chan", "@chan2", "t.me/chan3"]) == [
        "@chan",
        "@chan2",
        "@chan3",
    ]


def test_rejects_garbage_and_too_short():
    # "ab" короче 3 символов; "!!!" невалиден; пустые отбрасываются.
    assert normalize_warmup_channels(["ab", "!!!", "  ", "@good_one"]) == ["@good_one"]


def test_dedup_preserves_order():
    assert normalize_warmup_channels(["@aaa", "@bbb", "@aaa", "@ccc"]) == ["@aaa", "@bbb", "@ccc"]


def test_limit_caps_length():
    raw = [f"@chan{i}" for i in range(100)]
    out = normalize_warmup_channels(raw, limit=50)
    assert len(out) == 50
    assert out[0] == "@chan0"


def test_empty_input():
    assert normalize_warmup_channels(None) == []
    assert normalize_warmup_channels("") == []
    assert normalize_warmup_channels([]) == []


def test_profile_and_niche_constants_stable():
    # UI-опции и валидация эндпоинта должны совпадать с этими наборами.
    assert set(WARMUP_PROFILES) == {"reader", "commenter", "reactor", "lurker", "mixed"}
    assert "crypto" in WARMUP_NICHES and "general" in WARMUP_NICHES
