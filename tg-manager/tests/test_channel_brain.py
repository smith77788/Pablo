"""Channel Brain — редакционный quality gate постов (первый срез Virtual Administrator).

Проверяем детерминированную логику без БД/ИИ: похожесть/повторы, правила бренда,
чередование форматов и единый вердикт.
"""
from __future__ import annotations

from services import channel_brain as cb


# ── Похожесть / повторы ──────────────────────────────────────────────────────────

def test_jaccard_identical_and_disjoint():
    assert cb.jaccard("привет как дела друг", "привет как дела друг") == 1.0
    assert cb.jaccard("совершенно другой текст здесь", "молоко хлеб яйца масло") == 0.0


def test_repetition_flags_near_duplicate():
    recent = ["Сегодня запускаем новую услугу доставки по городу за час"]
    draft = "Сегодня запускаем новую услугу доставки по городу за час!"
    r = cb.repetition_check(draft, recent, dup_threshold=0.6)
    assert r["is_duplicate"] is True
    assert r["opening_repeat"] is True
    assert r["nearest_index"] == 0


def test_repetition_passes_distinct_text():
    recent = ["Как выбрать модель для рекламной съёмки"]
    draft = "Пять причин заказать доставку именно у нас в этот вторник"
    r = cb.repetition_check(draft, recent, dup_threshold=0.6)
    assert r["is_duplicate"] is False
    assert r["opening_repeat"] is False


def test_repetition_empty_recent():
    r = cb.repetition_check("любой текст", [], dup_threshold=0.6)
    assert r["is_duplicate"] is False and r["nearest_index"] == -1


# ── Правила бренда ────────────────────────────────────────────────────────────────

def test_brand_emoji_and_cta_limits():
    rules = cb.BrandRules(max_emoji=2, max_cta=1)
    draft = "Успей купить! 🔥🔥🔥 Подпишись и переходи по ссылке"
    res = cb.brand_check(draft, rules)
    assert res["passed"] is False
    codes = {v["code"] for v in res["violations"]}
    assert "too_many_emoji" in codes  # 3 эмодзи > 2
    assert "too_many_cta" in codes    # успей/купи/подпишись/переходи/по ссылке > 1


def test_brand_forbidden_and_opening_and_length():
    rules = cb.BrandRules(
        min_chars=10, max_chars=50,
        forbidden_words=("бесплатно",),
        banned_openings=("друзья,",),
    )
    res = cb.brand_check("Друзья, только сегодня всё бесплатно и выгодно очень", rules)
    codes = {v["code"] for v in res["violations"]}
    assert "forbidden_word" in codes
    assert "banned_opening" in codes
    assert "too_long" in codes
    assert res["passed"] is False


def test_brand_clean_passes():
    rules = cb.BrandRules(max_emoji=3, max_cta=2)
    res = cb.brand_check("Небольшая история о том, как мы ускорили доставку 🙂", rules)
    assert res["passed"] is True and res["violations"] == []


# ── Контент-микс ──────────────────────────────────────────────────────────────────

def test_pick_next_avoids_streak():
    # две рекламы подряд → следующая не должна быть рекламой
    nxt = cb.pick_next_pillar(["story", "ad", "ad"], ["ad", "story", "case"], max_streak=2)
    assert nxt != "ad"


def test_pick_next_fills_underrepresented():
    # 'case' ни разу не выходил при равных весах → берём его
    nxt = cb.pick_next_pillar(["ad", "story", "ad", "story"], ["ad", "story", "case"])
    assert nxt == "case"


def test_pick_next_empty_allowed():
    assert cb.pick_next_pillar(["ad"], []) is None


# ── Единый вердикт ────────────────────────────────────────────────────────────────

def test_gate_blocks_duplicate_for_review():
    recent = ["Запускаем доставку за час по всему городу уже сегодня"]
    v = cb.editorial_gate("Запускаем доставку за час по всему городу уже сегодня.",
                          recent_texts=recent)
    assert v.ok is False and v.needs_review is True
    assert any("повтор" in r for r in v.reasons)


def test_gate_passes_clean_draft():
    rules = cb.BrandRules(max_emoji=3, max_cta=2)
    v = cb.editorial_gate(
        "Короткая заметка: за неделю мы сократили среднее время доставки на 12 минут.",
        rules=rules,
        recent_texts=["Совсем другая тема про модельные кастинги в Милане"],
    )
    assert v.ok is True and v.needs_review is False and v.reasons == []


def test_gate_reports_brand_violation():
    rules = cb.BrandRules(max_cta=0)
    v = cb.editorial_gate("Купите сейчас со скидкой", rules=rules, recent_texts=[])
    assert v.needs_review is True
    assert any("призывов к действию" in r for r in v.reasons)


def test_gate_reasons_are_russian_not_codes():
    """Причины видит владелец: сырой код правила (too_many_emoji) — это английский в UI."""
    import re
    rules = cb.BrandRules(min_chars=500, max_emoji=0, max_cta=0,
                          forbidden_words=("халява",), banned_openings=("друзья",))
    v = cb.editorial_gate("Друзья, халява! Купите 🔥", rules=rules,
                          recent_texts=["Друзья, халява! Купите 🔥"])
    assert len(v.reasons) >= 6
    for r in v.reasons:
        assert not re.search(r"[A-Za-z_]{3,}", r), r
    assert any("совпадает на 100%" in r for r in v.reasons)
    # код для машин не теряется
    assert {x["code"] for x in v.brand["violations"]} >= {"too_short", "too_many_emoji"}
