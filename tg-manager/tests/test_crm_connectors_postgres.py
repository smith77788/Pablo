"""amoCRM/Bitrix24 на живой базе: триггер воронки → доставка → запросы в CRM →
смена этапа в CRM → этап у нас, и без эха обратно.

CRM подменена эмулятором API amoCRM (тот же набор адресов и форм ответа),
база — настоящая: триггеры schema_v272, связи crm_links, очередь доставок.
Нужен Postgres (INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993362
OTHER = 993363
BOT_ID = 993362001

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p):
    for o in (OWNER, OTHER):
        await p.execute("DELETE FROM webhook_endpoints WHERE owner_id=$1", o)
        await p.execute("DELETE FROM integration_events WHERE owner_id=$1", o)
        await p.execute("DELETE FROM contact_crm_activity WHERE owner_id=$1", o)
        await p.execute("DELETE FROM contact_crm WHERE owner_id=$1", o)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", o)
        await p.execute("DELETE FROM crm_deals WHERE owner_id=$1", o)
        await p.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", o)
        await p.execute("DELETE FROM automation_events WHERE owner_id=$1", o)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    assert _run(p.fetchval("SELECT to_regclass('crm_links') IS NOT NULL")), \
        "schema_v272_crm_connectors.sql не применилась"
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean_each(pool):
    _run(_cleanup(pool))
    yield


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    from services import security

    async def _ok(url):
        return security.is_safe_public_url(url)
    monkeypatch.setattr(security, "resolve_url_is_public", _ok)


# ── Эмулятор amoCRM ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = body

    async def read(self):
        return json.dumps(self._b).encode() if self._b is not None else b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAmo:
    PIPELINES = {"_embedded": {"pipelines": [
        {"id": 7, "name": "Продажи", "is_main": True, "_embedded": {"statuses": [
            {"id": 1, "name": "Неразобранное", "sort": 10, "type": 1},
            {"id": 10, "name": "Первичный контакт", "sort": 20},
            {"id": 11, "name": "Переговоры", "sort": 30},
            {"id": 12, "name": "Принимают решение", "sort": 40},
            {"id": 13, "name": "Договор", "sort": 50},
            {"id": 142, "name": "Успешно", "sort": 10000},
            {"id": 143, "name": "Не реализовано", "sort": 11000}]}},
        {"id": 8, "name": "Опт", "is_main": False, "_embedded": {"statuses": [
            {"id": 20, "name": "Заявка", "sort": 10},
            {"id": 142, "name": "Успешно", "sort": 10000},
            {"id": 143, "name": "Не реализовано", "sort": 11000}]}}]}}

    def __init__(self):
        self.contacts: dict[int, dict] = {}
        self.leads: dict[int, dict] = {}
        self.notes: list[tuple[int, str]] = []
        self.webhooks: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self._id = 1000

    def _next(self):
        self._id += 1
        return self._id

    def request(self, method, url, json=None, headers=None, **kw):
        assert headers.get("Authorization") == "Bearer " + "t" * 40
        path = url.split("amocrm.ru", 1)[1]
        self.calls.append((method, path))
        return _Resp(*self._handle(method, path, json))

    def _handle(self, method, path, body):
        if path == "/api/v4/account":
            return 200, {"name": "ACME"}
        if path == "/api/v4/leads/pipelines":
            return 200, self.PIPELINES
        if path == "/api/v4/webhooks":
            if method == "POST":
                self.webhooks.append(body["destination"])
            else:
                self.webhooks.remove(body["destination"])
            return 200, {}
        if path == "/api/v4/contacts" and method == "POST":
            cid = self._next()
            self.contacts[cid] = body[0]
            return 200, {"_embedded": {"contacts": [{"id": cid}]}}
        m = re.match(r"^/api/v4/contacts/(\d+)$", path)
        if m and method == "PATCH":
            self.contacts[int(m.group(1))].update(body)
            return 200, {}
        if path == "/api/v4/leads" and method == "POST":
            lid = self._next()
            self.leads[lid] = dict(body[0])
            return 200, {"_embedded": {"leads": [{"id": lid}]}}
        m = re.match(r"^/api/v4/leads/(\d+)(/notes)?$", path)
        if m:
            lid = int(m.group(1))
            if lid not in self.leads:
                return 404, None
            if m.group(2):
                self.notes.append((lid, body[0]["params"]["text"]))
                return 200, {}
            if method == "PATCH":
                self.leads[lid].update(body)
                return 200, {}
            lead = self.leads[lid]
            return 200, {"id": lid, "status_id": lead.get("status_id"),
                         "pipeline_id": lead.get("pipeline_id", 7), "price": lead.get("price", 0)}
        return 500, {"title": "не эмулируется " + path}


TOKEN = "t" * 40
PUBLIC = "https://infragram.example"


async def _connect(pool, amo, owner=OWNER):
    from services import crm_connectors as crm
    return await crm.connect(pool, amo, owner, "amocrm", domain="acme", token=TOKEN,
                             public_base=PUBLIC)


async def _contact(pool, owner=OWNER, name="Анна", phone="+79990001122"):
    cid = await pool.fetchval(
        """INSERT INTO unified_contacts (owner_id, first_name, username, phones)
           VALUES ($1, $2, 'anna_buyer', $3::jsonb) RETURNING id""",
        owner, name, json.dumps([phone]))
    return str(cid)


async def _drain(pool, http):
    from services import webhooks as wh
    for _ in range(5):
        await wh.fan_out(pool)
        if not await wh.deliver_due(pool, http):
            break


def _deliveries(pool, owner=OWNER):
    return _run(pool.fetch(
        """SELECT d.event, d.status, d.last_error FROM webhook_deliveries d
           WHERE d.owner_id=$1 ORDER BY d.id""", owner))


def test_connect_reads_pipeline_maps_stages_and_subscribes(pool):
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    assert c["kind"] == "amocrm" and c["account"] == "ACME"
    assert c["pipeline_id"] == "7"
    assert c["stage_map"] == {"lead": "10", "contact": "11", "proposal": "12",
                              "negotiation": "13", "won": "142", "lost": "143"}
    assert c["inbound_auto"] is True
    assert amo.webhooks == [c["inbound_url"]]
    assert c["inbound_url"].startswith(PUBLIC + "/webhook/crm/")
    # Токен не лежит открытым текстом и наружу не отдаётся.
    raw = _run(pool.fetchval("SELECT secret_enc FROM webhook_endpoints WHERE id=$1", c["id"]))
    assert TOKEN not in (raw or "")
    assert TOKEN not in json.dumps(c, ensure_ascii=False)
    # Вторую amoCRM тому же владельцу не подключить.
    from services import crm_connectors as crm
    with pytest.raises(ValueError):
        _run(_connect(pool, FakeAmo()))
    assert crm.CRM_LABELS["amocrm"] in str(crm.CRM_LABELS)


def test_pipeline_contact_becomes_contact_and_lead_once(pool):
    from services.contacts_hub import crm_engine
    amo = FakeAmo()
    _run(_connect(pool, amo))
    cid = _run(_contact(pool))
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "proposal", "deal_value": 45000,
                                                   "currency": "RUB"}))
    _run(_drain(pool, amo))
    assert len(amo.contacts) == 1 and len(amo.leads) == 1
    contact = next(iter(amo.contacts.values()))
    assert contact["custom_fields_values"][0]["values"][0]["value"] == "+79990001122"
    lead_id, lead = next(iter(amo.leads.items()))
    assert lead["status_id"] == 12 and lead["pipeline_id"] == 7 and lead["price"] == 45000
    assert any("@anna_buyer" in t for _, t in amo.notes)
    # Смена этапа у нас → PATCH той же сделки, не новая сделка.
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "negotiation"}))
    _run(_drain(pool, amo))
    assert len(amo.leads) == 1
    assert amo.leads[lead_id]["status_id"] == 13
    assert all(r["status"] == "delivered" for r in _deliveries(pool))


def test_crm_stage_change_comes_back_without_echo(pool):
    from services import crm_connectors as crm
    from services.contacts_hub import crm_engine
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    cid = _run(_contact(pool))
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "lead", "deal_value": 1000}))
    _run(_drain(pool, amo))
    lead_id = next(iter(amo.leads))
    # Менеджер закрыл сделку в amoCRM.
    amo.leads[lead_id]["status_id"] = 142
    token = c["inbound_url"].rsplit("/", 1)[1]
    form = {"leads[status][0][id]": str(lead_id), "leads[status][0][status_id]": "142",
            "account[subdomain]": "acme"}
    code, _ = _run(crm.handle_inbound(pool, amo, token, form))
    assert code == 200
    row = _run(pool.fetchrow("SELECT stage FROM contact_crm WHERE owner_id=$1", OWNER))
    assert row["stage"] == "won"
    acts = _run(pool.fetch("SELECT description FROM contact_crm_activity WHERE owner_id=$1", OWNER))
    assert any("amoCRM" in a["description"] for a in acts)
    # Наша запись породила pipeline.updated — в amoCRM он не уходит обратно.
    patches_before = [c for c in amo.calls if c[0] == "PATCH"]
    _run(_drain(pool, amo))
    assert [c for c in amo.calls if c[0] == "PATCH"] == patches_before
    # Повтор того же вебхука ничего не меняет.
    _run(crm.handle_inbound(pool, amo, token, form))
    assert _run(pool.fetchval("SELECT COUNT(*) FROM contact_crm_activity WHERE owner_id=$1",
                              OWNER)) == len(acts)


def test_inbound_does_not_trust_the_body(pool):
    """Этап берётся из CRM по API, а не из тела: подделанный запрос с чужим
    status_id ничего не меняет."""
    from services import crm_connectors as crm
    from services.contacts_hub import crm_engine
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    cid = _run(_contact(pool))
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "lead"}))
    _run(_drain(pool, amo))
    lead_id = next(iter(amo.leads))
    token = c["inbound_url"].rsplit("/", 1)[1]
    _run(crm.handle_inbound(pool, amo, token, {"leads[status][0][id]": str(lead_id),
                                                "leads[status][0][status_id]": "142"}))
    assert _run(pool.fetchval("SELECT stage FROM contact_crm WHERE owner_id=$1", OWNER)) == "lead"
    assert _run(crm.handle_inbound(pool, amo, "нет-такого", {}))[0] == 404
    code, _ = _run(crm.handle_inbound(pool, amo, token, {"account[subdomain]": "evil",
                                                         "leads[status][0][id]": str(lead_id)}))
    assert code == 403


def test_deleted_lead_is_recreated_on_retry(pool):
    from services.contacts_hub import crm_engine
    amo = FakeAmo()
    _run(_connect(pool, amo))
    cid = _run(_contact(pool))
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "lead"}))
    _run(_drain(pool, amo))
    old = next(iter(amo.leads))
    del amo.leads[old]
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "contact"}))
    _run(_drain(pool, amo))
    last = _deliveries(pool)[-1]
    assert last["status"] == "pending" and "удалили" in last["last_error"]
    _run(pool.execute("UPDATE webhook_deliveries SET next_attempt_at=now() WHERE owner_id=$1", OWNER))
    _run(_drain(pool, amo))
    assert len(amo.leads) == 1 and old not in amo.leads
    assert next(iter(amo.leads.values()))["status_id"] == 11
    assert len(amo.contacts) == 1, "контакт переиспользован, второго не заведено"


def test_simple_deal_trigger_and_inbound_vocabulary(pool):
    from services import crm_connectors as crm
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    # В базе встречаются оба словаря этапов crm_deals (schema_v90 и
    # inline-миграция мини-аппа) — коннектор обязан понимать любой.
    import asyncpg
    try:
        did = _run(pool.fetchval(
            "INSERT INTO crm_deals (owner_id, title, contact, stage, value) "
            "VALUES ($1, 'Окна в офис', '@ivan', 'contacted', 12000) RETURNING id", OWNER))
        want = "qualified"
    except asyncpg.CheckViolationError:
        did = _run(pool.fetchval(
            "INSERT INTO crm_deals (owner_id, title, contact, stage, value) "
            "VALUES ($1, 'Окна в офис', '@ivan', 'contact', 12000) RETURNING id", OWNER))
        want = "negotiation"
    _run(_drain(pool, amo))
    lead = next(iter(amo.leads.values()))
    assert lead["name"] == "Окна в офис" and lead["status_id"] == 11 and lead["price"] == 12000
    lead_id = next(iter(amo.leads))
    amo.leads[lead_id]["status_id"] = 13  # «Договор» → negotiation → qualified
    token = c["inbound_url"].rsplit("/", 1)[1]
    _run(crm.handle_inbound(pool, amo, token, {"leads[update][0][id]": str(lead_id)}))
    assert _run(pool.fetchval("SELECT stage FROM crm_deals WHERE id=$1", did)) == want


def test_bot_person_order_and_payment(pool):
    amo = FakeAmo()
    _run(_connect(pool, amo))
    _run(pool.execute(
        "INSERT INTO managed_bots (token, bot_id, username, added_by) VALUES ($1, $2, 'shop_bot', $3)",
        f"tok-{uuid.uuid4()}", BOT_ID, OWNER))
    _run(pool.execute(
        "INSERT INTO bot_users (bot_id, user_id, username, first_name) VALUES ($1, 555, 'petr', 'Пётр')",
        BOT_ID))
    from services import automation_events as bus
    _run(bus.emit(pool, OWNER, "contact.started", {"param": "promo", "new": True},
                  bot_id=BOT_ID, chat_id=555))
    _run(bus.emit(pool, OWNER, "contact.field_set", {"field": "phone", "value": "+79995554433"},
                  bot_id=BOT_ID, chat_id=555))
    oid = _run(pool.fetchval(
        """INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, customer_username,
               customer_name, items, total_cents, currency, status)
           VALUES ($1, $2, 555, 'petr', 'Пётр', $3::jsonb, 129000, 'RUB', 'confirmed')
           RETURNING id""", OWNER, BOT_ID,
        json.dumps([{"name": "Пицца", "qty": 2, "price_cents": 64500}])))
    _run(bus.emit(pool, OWNER, "order.created", {"order_id": oid, "summary": "Пицца × 2"},
                  bot_id=BOT_ID, chat_id=555))
    _run(bus.emit(pool, OWNER, "payment.received",
                  {"source": "order", "order_id": oid, "amount": 129000, "currency": "RUB"},
                  bot_id=BOT_ID, chat_id=555))
    _run(_drain(pool, amo))
    assert [r["status"] for r in _deliveries(pool)] == ["delivered"] * 4, _deliveries(pool)
    assert len(amo.contacts) == 1, "заказ привязан к тому же человеку из бота"
    contact = next(iter(amo.contacts.values()))
    assert contact["custom_fields_values"][0]["values"][0]["value"] == "+79995554433"
    assert len(amo.leads) == 2
    order_lead = [v for v in amo.leads.values() if v["name"] == f"Заказ №{oid}"][0]
    assert order_lead["price"] == 1290 and order_lead["status_id"] == 142
    texts = " ".join(t for _, t in amo.notes)
    assert "promo" in texts and "Пицца × 2" in texts and "1290.00 RUB" in texts


def test_backfill_targets_only_the_crm(pool):
    from services import crm_connectors as crm
    from services import webhooks as wh
    from services.contacts_hub import crm_engine
    cid = _run(_contact(pool))
    _run(crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "proposal"}))
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    http_ep = _run(wh.create_endpoint(pool, OWNER, kind="http", name="n8n",
                                      url="https://hooks.example.com/in"))
    n = _run(crm.backfill(pool, OWNER, c["id"]))
    assert n == 1
    assert _run(pool.fetchval("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=$1",
                              http_ep["id"])) == 0
    _run(_drain(pool, amo))
    assert len(amo.leads) == 1
    # Повторная выгрузка не заводит второй сделки.
    _run(crm.backfill(pool, OWNER, c["id"]))
    _run(_drain(pool, amo))
    assert len(amo.leads) == 1


def test_owner_isolation_and_disconnect(pool):
    from services import crm_connectors as crm
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    assert _run(crm.describe(pool, OTHER, c["id"])) is None
    with pytest.raises(LookupError):
        _run(crm.backfill(pool, OTHER, c["id"]))
    assert not _run(crm.disconnect(pool, amo, OTHER, c["id"], public_base=PUBLIC))
    assert _run(crm.disconnect(pool, amo, OWNER, c["id"], public_base=PUBLIC))
    assert amo.webhooks == [], "подписка в amoCRM снята"


def test_configure_switches_pipeline_and_validates_stages(pool):
    from services import crm_connectors as crm
    amo = FakeAmo()
    c = _run(_connect(pool, amo))
    out = _run(crm.configure(pool, amo, OWNER, c["id"], pipeline_id="8"))
    assert out["pipeline_id"] == "8" and out["stage_map"]["lead"] == "20"
    with pytest.raises(ValueError):
        _run(crm.configure(pool, amo, OWNER, c["id"], stage_map={"lead": "10"}))
    out = _run(crm.configure(pool, amo, OWNER, c["id"], stage_map={"lost": ""}))
    assert "lost" not in out["stage_map"]


def test_bitrix_inbound_requires_application_token(pool):
    from services import crm_connectors as crm

    class _B:
        def request(self, method, url, json=None, headers=None, **kw):
            m = url.rsplit("/", 1)[1]
            if m == "crm.category.list.json":
                return _Resp(200, {"result": {"categories": [{"id": 0, "name": "Общая"}]}})
            if m == "crm.status.list.json":
                return _Resp(200, {"result": [
                    {"STATUS_ID": "NEW", "NAME": "Новая", "EXTRA": {"SEMANTICS": "process"}},
                    {"STATUS_ID": "WON", "NAME": "Успех", "EXTRA": {"SEMANTICS": "success"}}]})
            return _Resp(200, {"result": True})

    c = _run(crm.connect(pool, _B(), OWNER, "bitrix24",
                         webhook_url="https://p.bitrix24.ru/rest/1/abc/", public_base=PUBLIC))
    assert c["url"] == "https://p.bitrix24.ru", "секрет вебхука не показывается"
    token = c["inbound_url"].rsplit("/", 1)[1]
    form = {"event": "ONCRMDEALUPDATE", "data[FIELDS][ID]": "1",
            "auth[application_token]": "app-secret"}
    assert _run(crm.handle_inbound(pool, _B(), token, form))[0] == 403, "токен не задан — не принимаем"
    _run(crm.configure(pool, _B(), OWNER, c["id"], inbound_secret="app-secret"))
    assert _run(crm.handle_inbound(pool, _B(), token, form))[0] == 200
    assert _run(crm.handle_inbound(pool, _B(), token, {**form, "auth[application_token]": "x"}))[0] == 403
