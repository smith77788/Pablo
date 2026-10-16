"""Сенсор намерений: регэксп и мультифраза в правилах (обратная совместимость)."""
from __future__ import annotations

import pytest

from services import intent_sensor
from services.intent_sensor import _match, _phrase_matches


def test_plain_substring_still_works():
    assert _phrase_matches("цена", "а какая цена вопроса?")
    assert not _phrase_matches("цена", "просто привет")


def test_multi_phrase_alternatives():
    p = "цена|стоимость|прайс"
    assert _phrase_matches(p, "сколько стоимость?")
    assert _phrase_matches(p, "дайте прайс")
    assert not _phrase_matches(p, "здравствуйте")


def test_regex_rule_matches():
    # 4+ цифр подряд рядом с "руб"
    assert _phrase_matches(r"re:\d{4,}\s*руб", "готов, 5000 руб устроит")
    assert not _phrase_matches(r"re:\d{4,}\s*руб", "стоит 50 руб")


def test_broken_regex_never_raises():
    # незакрытая группа — не должно падать, просто не матчит
    assert _phrase_matches("re:(unclosed", "любой текст") is False


def test_empty_regex_body_no_match():
    assert _phrase_matches("re:", "что угодно") is False


def test_match_filters_rules_list():
    rules = [
        {"phrase": "цена"},
        {"phrase": "купить|заказать"},
        {"phrase": r"re:\d{4,}"},
        {"phrase": ""},
    ]
    hits = _match("хочу заказать за 1200", rules)
    phrases = {r["phrase"] for r in hits}
    assert "купить|заказать" in phrases
    assert r"re:\d{4,}" in phrases
    assert "цена" not in phrases


class _FakePool:
    def __init__(self):
        self.inserted = None

    async def fetchval(self, q, *a):
        self.inserted = a
        return 1


def test_add_rule_preserves_regex_case_and_validates():
    import asyncio
    pool = _FakePool()
    # regex с верхним регистром сохраняется как есть (не ломаем \B и т.п.)
    rid = asyncio.run(intent_sensor.add_rule(pool, 1, r"re:\bЦена\b", "proposal", None))
    assert rid == 1
    # phrase — 3-й позиционный аргумент INSERT (owner_id, phrase, ...)
    assert pool.inserted[1] == r"re:\bЦена\b"


def test_add_rule_rejects_broken_regex():
    import asyncio
    pool = _FakePool()
    with pytest.raises(ValueError):
        asyncio.run(intent_sensor.add_rule(pool, 1, "re:(bad", "proposal", None))


def test_add_rule_lowercases_plain_phrase():
    import asyncio
    pool = _FakePool()
    asyncio.run(intent_sensor.add_rule(pool, 1, "ЦеНа", "proposal", None))
    assert pool.inserted[1] == "цена"
