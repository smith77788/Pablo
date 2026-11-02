"""Коннекторы amoCRM и Bitrix24 без базы: разбор ввода, соответствие этапов,
форма запросов к CRM, разбор входящего вебхука.

Живая цепочка (триггер → доставка → CRM → обратно) — в
tests/test_crm_connectors_postgres.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services import crm_connectors as crm


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Ввод владельца ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("mycompany", "mycompany.amocrm.ru"),
    ("MyCompany.amocrm.ru", "mycompany.amocrm.ru"),
    ("https://mycompany.amocrm.ru/leads/pipeline/1", "mycompany.amocrm.ru"),
    ("acme.kommo.com", "acme.kommo.com"),
    ("acme.amocrm.com", "acme.amocrm.com"),
])
def test_amo_domain_is_normalized(raw, want):
    assert crm.parse_amo_domain(raw) == want


@pytest.mark.parametrize("raw", ["", "evil.com", "a.amocrm.ru.evil.com", "127.0.0.1",
                                 "x_y.amocrm.ru", "https://evil.com/amocrm.ru"])
def test_amo_domain_rejects_foreign_hosts(raw):
    """Токен уходит заголовком на этот адрес — чужой хост увёл бы токен."""
    with pytest.raises(ValueError):
        crm.parse_amo_domain(raw)


def test_bitrix_webhook_is_normalized():
    assert crm.parse_bitrix_webhook("https://Portal.bitrix24.ru/rest/1/abc123") == \
        "https://portal.bitrix24.ru/rest/1/abc123/"
    # Метод в конце адреса из примера Bitrix24 отрезается.
    assert crm.parse_bitrix_webhook("https://p.bitrix24.ru/rest/12/k_9/profile.json") == \
        "https://p.bitrix24.ru/rest/12/k_9/"


@pytest.mark.parametrize("raw", ["", "http://p.bitrix24.ru/rest/1/abc/",
                                 "https://p.bitrix24.ru/crm/deal/", "не адрес"])
def test_bitrix_webhook_rejects_non_webhooks(raw):
    with pytest.raises(ValueError):
        crm.parse_bitrix_webhook(raw)


def test_money_formats_minor_units_and_stars():
    assert crm.money(129000, "RUB") == "1290.00 RUB"
    assert crm.money(50, "XTR") == "50 ⭐"
    assert crm.money("n/a", "USD") == "n/a USD"


# ── Этапы ────────────────────────────────────────────────────────────────────

_STAGES = [
    {"id": "10", "name": "Первичный контакт", "kind": "open"},
    {"id": "11", "name": "Переговоры", "kind": "open"},
    {"id": "12", "name": "Принимают решение", "kind": "open"},
    {"id": "13", "name": "Согласование договора", "kind": "open"},
    {"id": "142", "name": "Успешно реализовано", "kind": "won"},
    {"id": "143", "name": "Закрыто и не реализовано", "kind": "lost"},
]


def test_auto_map_spreads_open_stages_and_closes_on_won_lost():
    m = crm.auto_stage_map(_STAGES)
    assert m == {"lead": "10", "contact": "11", "proposal": "12", "negotiation": "13",
                 "won": "142", "lost": "143"}


def test_auto_map_short_pipeline_keeps_order():
    m = crm.auto_stage_map([{"id": "a", "kind": "open"}, {"id": "b", "kind": "open"},
                            {"id": "w", "kind": "won"}])
    assert m["lead"] == "a" and m["negotiation"] == "b" and m["won"] == "w"
    assert "lost" not in m
    order = ["a", "b"]
    seq = [order.index(m[st]) for st in ("lead", "contact", "proposal", "negotiation")]
    assert seq == sorted(seq), "воронка не должна идти назад"


def test_unmapped_crm_stage_resolves_to_nearest_left():
    cfg = {"stages": _STAGES + [{"id": "14", "name": "Счёт выставлен", "kind": "open"}],
           "stage_map": {"lead": "10", "proposal": "12", "won": "142", "lost": "143"}}
    cfg["stages"] = cfg["stages"][:4] + [cfg["stages"][-1]] + cfg["stages"][4:6]
    assert crm.stage_for_status(cfg, "10") == "lead"
    assert crm.stage_for_status(cfg, "11") == "lead", "между lead и proposal — всё ещё lead"
    assert crm.stage_for_status(cfg, "13") == "proposal"
    assert crm.stage_for_status(cfg, "14") == "proposal"
    assert crm.stage_for_status(cfg, 142) == "won"
    assert crm.stage_for_status(cfg, "999") is None, "этап чужой воронки"


def test_deal_vocabulary_round_trips():
    for deal_stage, st in crm.DEAL_TO_STAGE.items():
        assert st in crm.STAGES
        assert crm.STAGE_TO_DEAL[st] in crm.DEAL_TO_STAGE
    assert set(crm.STAGE_TO_DEAL) == set(crm.STAGES)


def test_stage_list_matches_the_pipeline_of_the_project():
    from services.contacts_hub.crm_engine import STAGE_LABELS
    assert tuple(STAGE_LABELS) == crm.STAGES


# ── Входящий вебхук ──────────────────────────────────────────────────────────

def test_amo_form_ids_are_collected_and_capped():
    form = {"leads[status][0][id]": "101", "leads[status][0][status_id]": "142",
            "leads[update][0][id]": "102", "leads[update][1][id]": "101",
            "account[subdomain]": "acme", "leads[status][1][id]": "x1"}
    assert crm._form_ids(form, "amocrm") == ["101", "102"]
    many = {f"leads[update][{i}][id]": str(1000 + i) for i in range(50)}
    assert len(crm._form_ids(many, "amocrm")) == crm._MAX_INBOUND_IDS


def test_bitrix_form_id():
    assert crm._form_ids({"data[FIELDS][ID]": "77", "event": "ONCRMDEALUPDATE"},
                         "bitrix24") == ["77"]
    assert crm._form_ids({}, "bitrix24") == []


# ── Запросы к CRM ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def read(self):
        return json.dumps(self._body).encode() if self._body is not None else b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, json=None, headers=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}, **kw})
        status, body = self.answers.pop(0)
        return _Resp(status, body)


def test_amo_create_lead_shape():
    http = _Http([(200, {"_embedded": {"leads": [{"id": 555}]}})])
    c = crm.AmoClient(http, "acme.amocrm.ru", "tok")
    ext = _run(c.create_deal(name="Анна — Infragram", price="1500.70", currency="RUB",
                             status="11", pipeline="7", contact_ext_id="99"))
    assert ext == "555"
    call = http.calls[0]
    assert call["url"] == "https://acme.amocrm.ru/api/v4/leads"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["allow_redirects"] is False, "токен не должен уехать по переадресации"
    lead = call["json"][0]
    assert lead["price"] == 1500 and lead["status_id"] == 11 and lead["pipeline_id"] == 7
    assert lead["_embedded"]["contacts"] == [{"id": 99}]


def test_amo_errors_are_russian_and_classified():
    c = crm.AmoClient(_Http([(401, {"title": "Unauthorized"})]), "a.amocrm.ru", "t")
    with pytest.raises(crm.CrmError) as e:
        _run(c.account_name())
    assert "токен" in e.value.message and e.value.code == 401
    c = crm.AmoClient(_Http([(404, None)]), "a.amocrm.ru", "t")
    with pytest.raises(crm.CrmError) as e:
        _run(c.update_deal("5", status="1", pipeline=None))
    assert e.value.not_found


def test_amo_pipelines_skip_unsorted_and_mark_closed():
    body = {"_embedded": {"pipelines": [{"id": 7, "name": "Продажи", "is_main": True,
             "_embedded": {"statuses": [
                 {"id": 1, "name": "Неразобранное", "sort": 10, "type": 1},
                 {"id": 10, "name": "Первичный контакт", "sort": 20, "type": 0},
                 {"id": 142, "name": "Успешно", "sort": 10000, "type": 0},
                 {"id": 143, "name": "Не реализовано", "sort": 11000, "type": 0}]}}]}}
    c = crm.AmoClient(_Http([(200, body)]), "a.amocrm.ru", "t")
    p = _run(c.pipelines())[0]
    assert [s["id"] for s in p["stages"]] == ["10", "142", "143"]
    assert [s["kind"] for s in p["stages"]] == ["open", "won", "lost"]


@pytest.fixture
def _public_dns(monkeypatch):
    from services import security

    async def _ok(url):
        return security.is_safe_public_url(url)
    monkeypatch.setattr(security, "resolve_url_is_public", _ok)


def test_bitrix_deal_and_contact_shape(_public_dns):
    http = _Http([(200, {"result": 31}), (200, {"result": 900})])
    c = crm.BitrixClient(http, "https://p.bitrix24.ru/rest/1/abc/")
    cid = _run(c.create_contact({"name": "Анна", "first_name": "Анна", "phone": "+7999",
                                 "username": "anna"}))
    did = _run(c.create_deal(name="Заказ №4", price=12.5, currency="rub", status="C1:NEW",
                             pipeline="1", contact_ext_id=cid))
    assert (cid, did) == ("31", "900")
    assert http.calls[0]["url"] == "https://p.bitrix24.ru/rest/1/abc/crm.contact.add.json"
    f = http.calls[0]["json"]["fields"]
    assert f["PHONE"][0]["VALUE"] == "+7999" and f["IM"][0]["VALUE_TYPE"] == "TELEGRAM"
    d = http.calls[1]["json"]["fields"]
    assert d == {"TITLE": "Заказ №4", "OPPORTUNITY": 12.5, "SOURCE_ID": "OTHER",
                 "SOURCE_DESCRIPTION": "Infragram", "CURRENCY_ID": "RUB",
                 "CATEGORY_ID": 1, "STAGE_ID": "C1:NEW", "CONTACT_ID": 31}


def test_bitrix_error_texts(_public_dns):
    c = crm.BitrixClient(_Http([(401, {"error": "NO_AUTH_FOUND"})]), "https://p.bitrix24.ru/rest/1/a/")
    with pytest.raises(crm.CrmError) as e:
        _run(c.get_deal("1"))
    assert "вебхук" in e.value.message
    c = crm.BitrixClient(_Http([(403, {"error": "insufficient_scope"})]), "https://p.bitrix24.ru/rest/1/a/")
    with pytest.raises(crm.CrmError) as e:
        _run(c.get_deal("1"))
    assert "CRM" in e.value.message


def test_bitrix_internal_address_is_refused(monkeypatch):
    """Адрес вебхука вводит владелец — внутренний адрес не должен стать
    способом ходить во внутреннюю сеть платформы."""
    from services import security

    async def _no(url):
        return False
    monkeypatch.setattr(security, "resolve_url_is_public", _no)
    http = _Http([])
    c = crm.BitrixClient(http, "https://p.bitrix24.ru/rest/1/a/")
    with pytest.raises(crm.CrmError):
        _run(c.get_deal("1"))
    assert http.calls == []


def test_bitrix_stage_semantics():
    cats = {"result": {"categories": [{"id": 0, "name": "Общая", "isDefault": True}]}}
    stages = {"result": [
        {"STATUS_ID": "NEW", "NAME": "Новая", "EXTRA": {"SEMANTICS": "process"}},
        {"STATUS_ID": "WON", "NAME": "Сделка успешна", "EXTRA": {"SEMANTICS": "success"}},
        {"STATUS_ID": "LOSE", "NAME": "Провал", "EXTRA": {"SEMANTICS": "failure"}},
        {"STATUS_ID": "APOLOGY", "NAME": "Анализ причины"}]}
    from services import security

    async def _ok(url):
        return True
    orig = security.resolve_url_is_public
    security.resolve_url_is_public = _ok
    try:
        c = crm.BitrixClient(_Http([(200, cats), (200, stages)]), "https://p.bitrix24.ru/rest/1/a/")
        p = _run(c.pipelines())
    finally:
        security.resolve_url_is_public = orig
    assert [s["kind"] for s in p[0]["stages"]] == ["open", "won", "lost", "lost"]


# ── Каталог ──────────────────────────────────────────────────────────────────

def test_default_crm_events_exist_in_catalog_and_have_handlers():
    from services import webhooks as wh
    for ev in crm.DEFAULT_EVENTS:
        assert ev in wh.EVENTS, ev
        assert ev in crm.HANDLERS, ev
    assert set(wh.CRM_KINDS) == set(crm.CRM_KINDS)


def test_unknown_event_is_a_noop_not_a_failure():
    ok, code, note = _run(crm.deliver(None, None, {"kind": "amocrm"}, "account.added", {}))
    assert ok and "пропущено" in note


def test_migration_allows_crm_kinds():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sql = open(os.path.join(root, "schema_v273_crm_connectors.sql"), encoding="utf-8").read()
    for k in crm.CRM_KINDS:
        assert f"'{k}'" in sql
