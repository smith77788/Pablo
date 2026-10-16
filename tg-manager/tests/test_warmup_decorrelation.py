"""Регресс: прогрев не создаёт сигнатуру когорты.

Первопричины:
1) Сессионный путь брал `_WARMUP_PUBLIC_CHANNELS[:8]` — одни и те же 8 каналов
   для КАЖДОГО аккаунта всего флота (а путь планов делал глобальный shuffle:
   набор тот же, порядок скакал каждый день). Совпадающий граф вступлений
   кластеризует когорту — прогрев сам выдавал ботнет.
2) Комментарии брались random.choice из 19 фиксированных фраз — весь флот писал
   один и тот же текст.
"""
from __future__ import annotations

import itertools
import os

os.environ.setdefault("MANAGER_BOT_TOKEN", "x")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/x")

from services.account_warmer import (  # noqa: E402
    _account_channels,
    _warm_comment_text,
    _COMMENT_SPINTAX,
    _WARMUP_PUBLIC_CHANNELS,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCS = list(range(1001, 1013))


def test_channel_sets_differ_between_accounts():
    """Главное: у разных аккаунтов РАЗНЫЕ наборы каналов."""
    sets = {a: _account_channels(a, _WARMUP_PUBLIC_CHANNELS, 8) for a in ACCS}
    pairs = list(itertools.combinations(ACCS, 2))
    identical = [(a, b) for a, b in pairs if sets[a] == sets[b]]
    assert not identical, f"совпадающие наборы каналов: {identical[:3]}"


def test_channel_overlap_is_far_below_full():
    """Пересечение наборов заметно меньше полного (было 8/8 — идентичны)."""
    sets = {a: set(_account_channels(a, _WARMUP_PUBLIC_CHANNELS, 8)) for a in ACCS}
    pairs = list(itertools.combinations(ACCS, 2))
    avg = sum(len(sets[a] & sets[b]) for a, b in pairs) / len(pairs)
    assert avg < 6.0, f"среднее пересечение слишком велико: {avg:.1f}/8"


def test_channel_set_is_stable_for_same_account():
    """Живой человек не перевыбирает интересы каждый день — набор стабилен."""
    first = _account_channels(1001, _WARMUP_PUBLIC_CHANNELS, 8)
    for _ in range(5):
        assert _account_channels(1001, _WARMUP_PUBLIC_CHANNELS, 8) == first


def test_channel_selection_edge_cases():
    assert _account_channels(1, [], 8) == []
    # без account_id — прежнее поведение (первые k), контракт не сломан
    assert _account_channels(None, _WARMUP_PUBLIC_CHANNELS, 3) == _WARMUP_PUBLIC_CHANNELS[:3]
    # k больше пула не ломает
    out = _account_channels(7, _WARMUP_PUBLIC_CHANNELS, 999)
    assert len(out) == len(_WARMUP_PUBLIC_CHANNELS)
    assert set(out) == set(_WARMUP_PUBLIC_CHANNELS)


def test_comments_are_generated_not_fixed_list():
    texts = {_warm_comment_text(a, m) for a in ACCS for m in range(3)}
    assert len(texts) > 10, f"мало разнообразия: {texts}"


def test_comment_is_deterministic_per_account_and_post():
    assert _warm_comment_text(1001, 42) == _warm_comment_text(1001, 42)


def test_comment_variants_are_natural():
    """Любая комбинация шаблона должна читаться грамотно (кривой текст —
    сам по себе детект-сигнал)."""
    from services.spintax_engine import SpintaxEngine

    eng = SpintaxEngine()
    variants = set()
    for t in _COMMENT_SPINTAX:
        variants |= {eng.generate(t, seed=f"s{i}") for i in range(40)}
    assert len(variants) > 40, f"вариантов мало: {len(variants)}"
    for v in variants:
        assert v.strip() == v and v, f"пустой/грязный вариант: {v!r}"
        assert "  " not in v, f"двойной пробел: {v!r}"
        assert "{" not in v and "|" not in v, f"незаменённый spintax: {v!r}"
        assert v[0].isupper(), f"вариант не с заглавной: {v!r}"


def test_wired_into_both_paths():
    with open(os.path.join(ROOT, "services/account_warmer.py"), encoding="utf-8") as f:
        src = f.read()
    # старые «одинаковые для всех» конструкции убраны из КОДА
    # (упоминания в комментариях, объясняющих прежнее поведение, допустимы)
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "targets = _WARMUP_PUBLIC_CHANNELS[:8]" not in code
    assert "comment = random.choice(_COMMENT_TEXTS)" not in code
    # генератор и пер-аккаунтный выбор реально подключены
    assert "_warm_comment_text(account_id" in src
    assert src.count("_account_channels(") >= 3  # определение + оба пути
    assert "_acc_targets" in src
