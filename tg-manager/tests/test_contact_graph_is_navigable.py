"""Граф связей контактов: числа ведут к парам, пары — к карточкам.

Экран «🕸 Граф связей» был из тех, на которые жаловался владелец: «только
отображают какие-то числа… ничего не кликабельно, ничего не настраивается». Он
печатал счётчик «Связей», список «По типам» с сырым ключом столбца
(`same_username_pattern`) и десять строк «Имя ↔ Имя» — и ни одного перехода. При
пустой базе вместо подсказки показывал ноль, а при ошибке — текст без «Повторить».

Тест держит рабочее состояние: подписи типов — по-русски и общие с карточкой
контакта, счётчик по типу открывает список пар, каждая сторона пары открывает
карточку контакта, пустое состояние предлагает рассчитать связи, ошибка —
повторить. И бэкенд, который это обслуживает: выборка пар одного типа,
скоупленная по владельцу с ОБЕИХ сторон.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
ENGINE = open(os.path.join(ROOT, "services", "contacts_hub", "relationship_engine.py"),
              encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    """Тело JS-функции по имени — по балансу фигурных скобок."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(f"не закрылась функция {name}")


# ── подписи ──────────────────────────────────────────────────────────────────

def test_relationship_labels_are_shared_and_russian():
    assert "const UCH_REL_RU" in HTML, "нет общего словаря подписей типов связи"
    assert "function uchRelRu(" in HTML
    for key in ("phone_match", "same_username_pattern", "same_company",
                "mutual_account"):
        assert re.search(r"\n  " + key + r": '[^']*[А-Яа-яЁё]", HTML), \
            f"нет русской подписи для типа {key}"
    assert "Похожий username" not in HTML, "английское слово в подписи типа связи"


def test_graph_screen_does_not_print_raw_type_key():
    body = _fn("openUchGraph")
    assert "esc(t.relationship_type)" not in body, \
        "экран печатает сырой ключ типа вместо подписи"
    assert "uchRelRu(t.relationship_type)" in body
    pair = _fn("_uchPairRow")
    assert "esc(r.relationship_type)" not in pair
    assert "uchRelRu(r.relationship_type)" in pair


def test_contact_relationships_uses_the_shared_labels():
    body = _fn("openContactRelationships")
    assert "typeLabels" not in body, "локальный словарь подписей вернулся"
    assert "uchRelRu(r.relationship_type)" in body


# ── переходы ─────────────────────────────────────────────────────────────────

def test_type_counter_opens_the_pairs():
    body = _fn("openUchGraph")
    assert "openUchGraphType(" in body, "счётчик по типу никуда не ведёт"
    assert 'id="s-uchgraphtype"' in HTML, "нет экрана списка пар"
    assert "async function openUchGraphType(" in HTML


def test_pair_opens_both_contact_cards():
    pair = _fn("_uchPairRow")
    assert pair.count("openContactDetail(") == 2, \
        "из пары нельзя открыть обе карточки контактов"
    assert "r.contact_a_id" in pair and "r.contact_b_id" in pair


def test_contact_relationship_row_opens_the_other_side():
    body = _fn("openContactRelationships")
    assert "openContactDetail(" in body, "связь не ведёт ко второму контакту"
    assert "contact_a_id" in body and "contact_b_id" in body


def test_pair_shows_why_it_is_a_pair():
    assert "function _uchPairWhy(" in HTML
    why = _fn("_uchPairWhy")
    for field in ("phone", "username", "company", "account_id"):
        assert "m." + field in why, f"причина связи не показывает {field}"


# ── состояния ────────────────────────────────────────────────────────────────

def test_graph_has_an_empty_state_with_an_action():
    body = _fn("openUchGraph")
    assert "empty(" in body, "нет пустого состояния: при нуле связей экран пустой"
    assert "computeGraph()" in body, "пустое состояние без кнопки «Рассчитать»"


def test_relationships_empty_state_has_an_action():
    body = _fn("openContactRelationships")
    assert "computeGraph()" in body, "«Нет связей» без действия — тупик"


def test_errors_offer_a_retry():
    for name in ("openUchGraph", "openUchGraphType", "openContactRelationships"):
        body = _fn(name)
        m = re.search(r"errHtml\(errRu\(e\)([^)]*)\)", body)
        assert m, f"{name}: ошибка показывается не через errHtml"
        assert m.group(1).strip().startswith(","), \
            f"{name}: ошибка без кнопки «Повторить»"


# ── бэкенд ───────────────────────────────────────────────────────────────────

def test_pairs_endpoint_is_registered():
    assert "GET /api/miniapp/uch/graph/pairs" in SNAPSHOT


def test_pairs_query_is_owner_scoped_on_both_sides():
    m = re.search(r"async def get_pairs_by_type\(.*?\n    rows = await pool\.fetch\(\s*'''(.*?)'''",
                  ENGINE, re.S)
    assert m, "нет выборки пар одного типа"
    sql = m.group(1)
    assert sql.count("owner_id = $1") >= 3, \
        "выборка пар не скоуплена по владельцу с обеих сторон"


def test_strongest_pairs_carry_ids_and_a_usable_name():
    m = re.search(r"strongest = await pool\.fetch\(\s*'''(.*?)'''", ENGINE, re.S)
    assert m
    sql = m.group(1)
    assert "r.contact_a_id" in sql and "r.contact_b_id" in sql
    assert "uc1.username" in sql, "имя пары без запасного варианта — экран печатал пустоту"
    assert sql.count("owner_id = $1") >= 3


def test_get_pairs_by_type_decodes_metadata_and_respects_limit():
    import services.contacts_hub.relationship_engine as RE

    seen = {}

    class _Pool:
        async def fetch(self, sql, *args):
            seen["sql"], seen["args"] = sql, args
            return [{"contact_a_id": "a", "contact_b_id": "b",
                     "relationship_type": "phone_match", "strength": 0.95,
                     "metadata": json.dumps({"phone": "+70000000000"}),
                     "a_name": "Артур", "b_name": "Пабло"}]

    out = asyncio.run(RE.get_pairs_by_type(_Pool(), 77, "phone_match", 25))
    assert seen["args"] == (77, "phone_match", 25)
    assert out[0]["metadata"] == {"phone": "+70000000000"}, \
        "metadata осталась строкой — экран не покажет причину связи"


def test_get_pairs_by_type_survives_broken_metadata():
    import services.contacts_hub.relationship_engine as RE

    class _Pool:
        async def fetch(self, sql, *args):
            return [{"contact_a_id": "a", "contact_b_id": "b",
                     "relationship_type": "same_company", "strength": 0.6,
                     "metadata": "{не json", "a_name": "", "b_name": ""}]

    out = asyncio.run(RE.get_pairs_by_type(_Pool(), 1, "same_company", 50))
    assert out[0]["metadata"] == {}
