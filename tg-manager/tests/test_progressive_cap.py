"""Прогрессивный объём инвайта на аккаунт по возрасту/доверию (чистая функция)."""
from __future__ import annotations

from services.flood_engine import progressive_cap, _INVITE_LIMIT_CEILING


def test_age_tiers():
    assert progressive_cap(1) == 5        # < 24ч — свежий
    assert progressive_cap(48) == 12      # < 72ч
    assert progressive_cap(100) == 25     # < 168ч (неделя)
    assert progressive_cap(200) == _INVITE_LIMIT_CEILING  # старше недели → потолок
    assert progressive_cap(None) == 5     # неизвестный возраст → как свежий


def test_trust_downscales():
    # низкое доверие срезает объём
    assert progressive_cap(200, trust=0.2) == _INVITE_LIMIT_CEILING // 2
    assert progressive_cap(200, trust=0.4) == int(_INVITE_LIMIT_CEILING * 0.75)
    # нормальное доверие — без среза
    assert progressive_cap(200, trust=1.0) == _INVITE_LIMIT_CEILING
    assert progressive_cap(48, trust=0.2) == 6   # 12//2


def test_never_below_one_or_above_ceiling():
    assert progressive_cap(0, trust=0.0) >= 1
    assert progressive_cap(10_000, trust=1.0) <= _INVITE_LIMIT_CEILING
