"""Одна доска сделок: воронка контактов и простые сделки — в одних колонках.

Сделок в продукте было две системы, и обе были видны владельцу отдельными
экранами с разными числами: «CRM — Сделки» (crm_deals — их пишут экран,
сценарии, автоответчик и коннекторы CRM; суммы с «$», нарисованным вручную,
даже у рублёвой сделки) и «Канбан сделок» (воронка контактов, contact_crm,
своя валюта у строки). «Лид 3» на одном экране и «Лид 10» на другом — и куда
смотреть, непонятно.

Теперь доска одна. Проверяется на настоящей базе, потому что запросы новые:

* простые сделки лежат в колонке своего этапа рядом с контактами, со своим
  счётом и признаком продолжения;
* сумма колонки складывается из обеих систем ПО ВАЛЮТАМ, а не в одно число;
* сделка без валюты (старая строка) считается в основной валюте владельца;
* сделка на этапе вне списка не прячется — у неё появляется колонка;
* чужие сделки на доску не попадают;
* создание сделки пишет выбранную валюту, а без выбора — основную валюту.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

ME, OTHER = 7412345678, 555

_SCHEMA = [
    """CREATE TABLE unified_contacts(id uuid PRIMARY KEY, owner_id bigint NOT NULL,
           display_name text, first_name text, last_name text, username text, company text)""",
    """CREATE TABLE contact_crm(id serial PRIMARY KEY, owner_id bigint NOT NULL,
           contact_id uuid NOT NULL, stage text, deal_value numeric(14,2),
           currency text DEFAULT 'USD', next_reminder_at timestamptz,
           next_reminder_text text, last_interaction_at timestamptz,
           last_message_preview text, updated_at timestamptz DEFAULT now())""",
    """CREATE TABLE crm_deals(id serial PRIMARY KEY, owner_id bigint NOT NULL,
           title text NOT NULL, contact text, stage text NOT NULL DEFAULT 'lead',
           value numeric(14,2) DEFAULT 0, notes text,
           created_at timestamptz NOT NULL DEFAULT now(),
           updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE owner_money_settings(owner_id bigint PRIMARY KEY, currency text,
           manual_rates jsonb)""",
]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


async def _with_db(body):
    import asyncpg
    name = "deals_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(_db(name), min_size=1, max_size=2)
        for ddl in _SCHEMA:
            await pool.execute(ddl)
        # Миграция из репозитория, а не копия её текста.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "schema_v292_crm_deal_currency.sql"), encoding="utf-8") as f:
            await pool.execute(f.read())
        return await body(pool)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


async def _seed(pool):
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    await pool.execute("INSERT INTO owner_money_settings VALUES($1,'RUB',NULL)", ME)
    await pool.executemany(
        "INSERT INTO unified_contacts(id, owner_id, display_name) VALUES($1,$2,$3)",
        [(c1, ME, "Анна"), (c2, ME, "Борис")])
    await pool.executemany(
        "INSERT INTO contact_crm(owner_id, contact_id, stage, deal_value, currency) "
        "VALUES($1,$2,$3,$4,$5)",
        [(ME, c1, "lead", 1000, "RUB"), (ME, c2, "lead", 50, "USD")])
    await pool.executemany(
        "INSERT INTO crm_deals(owner_id, title, stage, value, currency) VALUES($1,$2,$3,$4,$5)",
        [(ME, "Подписка", "lead", 2000, "RUB"),
         (ME, "Старая сделка без валюты", "lead", 300, None),
         (ME, "Внедрение", "lead", 70, "USD"),
         (ME, "Старый этап", "archived", 5, "RUB"),
         (OTHER, "Чужая", "lead", 999999, "RUB")])


def test_simple_deals_sit_in_the_same_columns_and_money_adds_up_by_currency():
    from services import mini_app_deals_board as B

    async def body(pool):
        await _seed(pool)
        main = await B.owner_main_currency(pool, ME)
        return main, await B.load_board(pool, ME, per_stage=2, main_currency=main)

    main, b = _run(_with_db(body))
    assert main == "RUB"
    cols = {c["stage"]: c for c in b["columns"]}
    lead = cols["lead"]
    assert lead["count"] == 2, "контакты в воронке посчитаны неверно"
    assert lead["deal_count"] == 3, "простые сделки не дошли до колонки своего этапа"
    assert len(lead["deals"]) == 2 and lead["deals_has_more"] is True, (
        "колонка с потолком молчит о продолжении")
    # 1000 (контакт) + 2000 + 300 (без валюты → основная) рублей; 50 + 70 долларов.
    assert lead["by_currency"] == {"RUB": 3300.0, "USD": 120.0}, lead["by_currency"]
    assert "archived" in cols and cols["archived"]["deal_count"] == 1, (
        "сделка на этапе вне списка спряталась: в итоге есть, в колонках нет")
    assert b["deals_total"] == 4, "в итог попала чужая сделка или потерялась своя"
    titles = [d["title"] for c in b["columns"] for d in c["deals"]]
    assert "Чужая" not in titles
    assert all(d["currency"] for c in b["columns"] for d in c["deals"])


def test_the_next_page_of_a_column_comes_with_currency():
    from services import mini_app_api as M

    class _Req:
        def __init__(self, **q):
            self.rel_url = type("U", (), {"query": dict(q)})()
            self.query = dict(q)
            self.headers = {}
            self.match_info = {}

    async def body(pool):
        await _seed(pool)
        from aiohttp import web
        # Ручки берут _get_uid при регистрации — подмена ДО setup_routes.
        orig = M._get_uid
        M._get_uid = lambda r: ME
        try:
            app = web.Application()
            M.setup_routes(app, pool)
            h = next(r.handler for r in app.router.routes()
                     if r.method == "GET" and r.resource.canonical == "/api/miniapp/crm/deals")
            resp = await h(_Req(stage="lead", offset="2", limit="2"))
        finally:
            M._get_uid = orig
        return json.loads(resp.body.decode())

    d = _run(_with_db(body))
    assert d["total"] == 3 and len(d["deals"]) == 1 and d["page"]["has_more"] is False
    deal = d["deals"][0]
    assert deal["currency"] in ("RUB", "USD") and deal["deal_id"] == deal["id"]


def test_create_writes_the_chosen_currency_or_the_owner_main_one():
    from services import mini_app_api as M

    class _Req:
        def __init__(self, payload):
            self._p = payload
            self.rel_url = type("U", (), {"query": {}})()
            self.query = {}
            self.headers = {}
            self.match_info = {}

        async def json(self):
            return self._p

    async def body(pool):
        await pool.execute("INSERT INTO owner_money_settings VALUES($1,'KZT',NULL)", ME)
        from aiohttp import web
        # Ручки берут _get_uid при регистрации — подмена ДО setup_routes.
        orig = M._get_uid
        M._get_uid = lambda r: ME
        try:
            app = web.Application()
            M.setup_routes(app, pool)
            h = next(r.handler for r in app.router.routes()
                     if r.method == "POST" and r.resource.canonical == "/api/miniapp/crm/deal")
            r1 = await h(_Req({"title": "В евро", "value": 10, "currency": "eur"}))
            r2 = await h(_Req({"title": "Без валюты", "value": 10}))
            bad = await h(_Req({"title": "Мусор", "value": 10, "currency": "XYZ"}))
            neg = await h(_Req({"title": "Минус", "value": -5}))
        finally:
            M._get_uid = orig
        rows = await pool.fetch("SELECT title, currency FROM crm_deals ORDER BY id")
        return r1.status, r2.status, bad.status, neg.status, [tuple(r) for r in rows]

    s1, s2, sb, sn, rows = _run(_with_db(body))
    assert (s1, s2) == (200, 200)
    assert sb == 400, "незнакомая валюта легла в базу"
    assert sn == 400, "отрицательная сумма сделки легла в базу"
    assert rows == [("В евро", "EUR"), ("Без валюты", "KZT")], rows
