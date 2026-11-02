"""Эталонные посты виртуального администратора: что редакционный гейт пропускает
сам, а что обязан отдать человеку и почему.

Набор — tests/fixtures/admin_posts_golden.json. Если правка
services/channel_brain начнёт пропускать запрещённое слово, почти повтор или
заваленный эмодзи пост — или, наоборот, глушить чистый текст, тест покраснеет
на конкретном черновике.
"""
from __future__ import annotations

import json
import os

import pytest

from services import channel_brain as cb

_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "admin_posts_golden.json")
with open(_PATH, encoding="utf-8") as _f:
    GOLDEN = json.load(_f)

_R = GOLDEN["rules"]
RULES = cb.BrandRules(min_chars=_R["min_chars"], max_chars=_R["max_chars"],
                      max_emoji=_R["max_emoji"], max_cta=_R["max_cta"],
                      forbidden_words=tuple(_R["forbidden_words"]),
                      banned_openings=tuple(_R["banned_openings"]))


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["id"])
def test_черновик_получает_ожидаемый_вердикт(case):
    v = cb.editorial_gate(case["draft"], rules=RULES, recent_texts=GOLDEN["recent"])
    assert v.ok is case["ok"], f"{case['id']}: причины {v.reasons}"
    codes = {x["code"] for x in v.brand.get("violations", [])}
    for code in case.get("codes", []):
        assert code in codes, f"{case['id']}: нет нарушения {code}, есть {codes}"
    for frag in case.get("reasons", []):
        assert any(frag in r for r in v.reasons), f"{case['id']}: нет причины «{frag}» в {v.reasons}"


def test_набор_покрывает_каждое_правило():
    codes = {c for case in GOLDEN["cases"] for c in case.get("codes", [])}
    assert {"too_short", "too_long", "too_many_emoji", "too_many_cta",
            "forbidden_word", "banned_opening"} <= codes
    assert sum(1 for c in GOLDEN["cases"] if c["ok"]) >= 3, "нужны и чистые посты"
