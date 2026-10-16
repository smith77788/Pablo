"""Регресс: прогрессивный гейт действий прогрева по ЗРЕЛОСТИ и здоровью.

Первопричина: гейт зависел только от trust_score, но trust_score в проекте —
сигнал ЗДОРОВЬЯ: здоровый аккаунт получает 1.0 с первого дня (trust_engine
считает score = 1.0 + age_bonus − штрафы, т.е. база равна максимуму). Значит
гейт фактически не ограничивал ничего — свежий аккаунт с нуля писал
комментарии и ставил реакции. В пути одиночных планов гейта не было вовсе.

Теперь ограничивает СТРОГИЙ из двух сигналов: зрелость (возраст + день
прогрева) и здоровье (trust_score).
"""
from __future__ import annotations

import os

os.environ.setdefault("MANAGER_BOT_TOKEN", "x")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/x")

from services.account_warmer import (  # noqa: E402
    _progressive_actions,
    _get_actions_for_day,
    _maturity_level,
    _health_level,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISKY = ("send_comment", "send_reaction", "forward_to_saved")


def _full():
    return list(_get_actions_for_day(30))


def test_fresh_account_gets_only_safe_actions():
    """Ключевой сценарий: свежий здоровый аккаунт больше НЕ пишет комментарии."""
    out = _progressive_actions(_full(), trust_score=1.0, age_days=1, warmup_day=0)
    assert not [a for a in out if a in RISKY], f"свежему разрешены рискованные: {out}"
    assert "send_comment" not in out


def test_maturity_unlocks_gradually():
    full = _full()
    d5 = _progressive_actions(full, trust_score=1.0, age_days=5, warmup_day=5)
    d10 = _progressive_actions(full, trust_score=1.0, age_days=10, warmup_day=10)
    d30 = _progressive_actions(full, trust_score=1.0, age_days=30, warmup_day=30)
    assert "send_comment" not in d5 and "send_reaction" not in d5
    assert "send_reaction" in d10 and "send_comment" not in d10
    assert "send_comment" in d30
    assert len(d5) < len(d10) < len(d30)


def test_mature_healthy_account_not_restricted_no_regression():
    """Зрелый здоровый аккаунт получает полный набор — поведение не ухудшилось."""
    full = _full()
    out = _progressive_actions(full, trust_score=1.0, age_days=30, warmup_day=30)
    assert set(out) == set(full)


def test_health_still_restricts_mature_account():
    """Больной аккаунт урезается независимо от зрелости (здоровье не потеряно)."""
    out = _progressive_actions(_full(), trust_score=0.2, age_days=30, warmup_day=30)
    assert not [a for a in out if a in RISKY]


def test_strictest_of_two_signals_wins():
    full = _full()
    # молодой но здоровый — ограничен зрелостью
    assert "send_comment" not in _progressive_actions(
        full, trust_score=1.0, age_days=1, warmup_day=0)
    # зрелый но больной — ограничен здоровьем
    assert "send_comment" not in _progressive_actions(
        full, trust_score=0.2, age_days=99, warmup_day=99)


def test_unknown_data_does_not_restrict():
    """Нет данных о возрасте/здоровье → не ограничиваем (без регрессии)."""
    full = _full()
    assert set(_progressive_actions(full)) == set(full)


def test_never_returns_empty_list():
    """Цепочка фильтров могла обнулить набор → random.choice() падал IndexError."""
    assert _progressive_actions([], age_days=1, warmup_day=0)
    assert _progressive_actions(["send_comment"], age_days=1, warmup_day=0)
    assert _progressive_actions(["send_comment"], trust_score=0.1)


def test_level_helpers():
    assert _maturity_level(1, 0) == 0
    assert _maturity_level(5, 5) == 1
    assert _maturity_level(10, 10) == 2
    assert _maturity_level(30, 30) == 3
    assert _maturity_level(None, None) == 3  # неизвестно → не ограничиваем
    assert _health_level(0.2) == 0
    assert _health_level(0.45) == 1
    assert _health_level(0.6) == 2
    assert _health_level(1.0) == 3
    assert _health_level(None) == 3


def test_gate_applied_in_both_warmup_paths():
    with open(os.path.join(ROOT, "services/account_warmer.py"), encoding="utf-8") as f:
        src = f.read()
    # два пути: одиночные планы и мультиаккаунтные сессии
    assert src.count("_progressive_actions(") >= 3  # определение + 2 вызова
    # старый trust-only гейт удалён
    assert "Progressive trust: ограничиваем действия по trust_score" not in src
