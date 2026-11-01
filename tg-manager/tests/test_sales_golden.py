"""Эталонный набор ИИ-продавца: проверки ответа и подбор знаний под вопрос.

Набор живёт в tests/fixtures/sales_golden.json и общий с живым прогоном на
настоящей модели (deploy/scripts/ai_sales_eval.py). Здесь — детерминированная
часть: каждая ситуация говорит, какой ответ модели клиенту отдавать можно, а
какой проверка обязана задержать. Если правка services/sales_guard начнёт
пропускать выдуманную цену или глушить правильный ответ, тест покраснеет на
конкретной ситуации.
"""
from __future__ import annotations

import json
import os

import pytest

from services import sales_guard as g

_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sales_golden.json")
with open(_PATH, encoding="utf-8") as _f:
    GOLDEN = json.load(_f)
CAT = GOLDEN["catalog"]


def _verdict(text: str) -> str | None:
    if g.check_prices(text, CAT["persona"], CAT["products"], CAT["delivery"], CAT["promos"]):
        return "price"
    if g.check_discount(text, CAT["persona"], CAT["promos"]):
        return "discount"
    return None


_REPLIES = [(c["id"], r) for c in GOLDEN["cases"] for r in c["replies"]]


@pytest.mark.parametrize("case_id,reply", _REPLIES,
                         ids=[f"{cid}:{'ok' if r['ok'] else r['guard']}" for cid, r in _REPLIES])
def test_эталонный_ответ_получает_ожидаемый_вердикт(case_id, reply):
    got = _verdict(reply["text"])
    want = None if reply["ok"] else reply["guard"]
    assert got == want, f"{case_id}: «{reply['text']}» — ждали {want}, получили {got}"


@pytest.mark.parametrize("item", GOLDEN["injections"], ids=lambda i: i["text"][:30])
def test_попытка_переписать_правила_узнаётся(item):
    assert bool(g.detect_injection(item["text"])) is item["hit"], item["text"]


@pytest.mark.parametrize("item", GOLDEN["retrieval"], ids=lambda i: i["query"][:30])
def test_нужный_товар_встаёт_первым(item):
    ranked = g.rank_by_relevance(CAT["products"], item["query"])
    assert ranked[0]["name"] == item["top"]


def test_без_совпадений_порядок_владельца_не_меняется():
    names = [p["name"] for p in CAT["products"]]
    assert [p["name"] for p in g.rank_by_relevance(CAT["products"], "здравствуйте")] == names


def test_нужный_товар_доходит_до_модели_в_большом_каталоге():
    """Промпт берёт первые 60 позиций. Товар на сотом месте без подбора к модели
    не попадал вовсе, и она отвечала «такого нет» или выдумывала цену."""
    from services.bot_sales_persona import build_system_prompt
    filler = [{"name": f"Товар номер {i}", "price_cents": 100 + i} for i in range(120)]
    catalog = filler + [{"name": "Пицца Четыре сыра", "price_cents": 1590}]
    assert "Четыре сыра" not in build_system_prompt({"name": "Аня"}, catalog)
    ranked = g.rank_by_relevance(catalog, "сколько стоит пицца четыре сыра?")
    assert "Четыре сыра" in build_system_prompt({"name": "Аня"}, ranked)


def test_проверка_большого_каталога_укладывается_в_доли_секунды():
    import time
    big = [{"name": f"t{i}", "price_cents": 100 + i * 7} for i in range(300)]
    started = time.monotonic()
    g.check_prices("цена 99999 USD", {"discount_max_percent": 30}, big,
                   promos=[{"percent": 15}])
    assert time.monotonic() - started < 1.0
