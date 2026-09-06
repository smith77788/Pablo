"""Сущность бота — живой менеджер по продажам.

Чистые тесты (промпт из настроек, детект намерений, цены) идут в CI. Полный
жизненный цикл (персона→товары→назначение боту→заказ/оператор→память) — на живом
Postgres (INFRAGRAM_TEST_DSN): JSONB/partial-unique заглушкой пула не проверяются.
"""
from __future__ import annotations

import json
import os

import pytest

from services import bot_sales_persona as bsp


# ── Чистые ────────────────────────────────────────────────────────────────────
def test_format_price():
    assert bsp.format_price(999, "USD") == "9.99 USD"
    assert bsp.format_price(4500, "EUR") == "45.00 EUR"
    assert bsp.format_price(0) == "0.00 USD"


def test_detect_actions():
    p = {"handoff_triggers": "возврат, жалоба"}
    assert bsp.detect_actions("хочу оператора", p)["handoff"] is True
    assert bsp.detect_actions("это возврат денег", p)["handoff"] is True
    assert bsp.detect_actions("расскажи про кофе", p)["handoff"] is False
    assert bsp.detect_actions("хочу заказать 2 шт", p)["order_intent"] is True
    assert bsp.detect_actions("просто смотрю", p)["order_intent"] is False
    assert bsp.detect_actions("мой тел +7 900 123-45-67", p)["phone"]
    assert bsp.detect_actions("привет", p)["phone"] is None


def test_build_system_prompt_reflects_settings_and_real_prices():
    persona = {
        "name": "Анна", "role_title": "старший менеджер", "company_name": "CoffeeShop",
        "personality": "тёплая и внимательная", "tone": "warm", "formality": "vy",
        "emoji_level": "high", "msg_length": "short", "humor_level": 2,
        "disclose_prices": True, "taboo_topics": "политика, религия",
        "operator_username": "@op", "can_take_orders": True,
        "channels": [{"title": "Канал", "url": "https://t.me/x"}],
        "order_fields": ["Имя", "Телефон"], "guardrails": "не обещай сроки",
    }
    products = [{"name": "Эспрессо", "price_cents": 999, "currency": "USD",
                 "in_stock": True, "is_active": True, "description": "арабика"}]
    sp = bsp.build_system_prompt(persona, products)
    assert "Анна" in sp and "старший менеджер" in sp and "CoffeeShop" in sp
    assert "тёплая и внимательная" in sp
    assert "«вы»" in sp                              # formality
    assert "9.99 USD" in sp                          # реальная цена из каталога
    assert "выдумыв" in sp                           # анти-галлюцинация цен
    assert "политика, религия" in sp                 # табу
    assert "@op" not in sp or "специалист" in sp     # инструкция про оператора
    assert "https://t.me/x" in sp                    # каналы
    assert "Телефон" in sp                           # поля заказа
    assert "не обещай сроки" in sp                   # гардрейлы
    # НЕ раскрывает, что это бот
    assert "РЕАЛЬН" in sp.upper()


def test_build_prompt_hides_prices_when_disabled():
    persona = {"name": "X", "disclose_prices": False}
    products = [{"name": "T", "price_cents": 500, "currency": "USD",
                 "in_stock": True, "is_active": True}]
    sp = bsp.build_system_prompt(persona, products)
    assert "5.00" not in sp and "уточните у оператора" in sp


# ── Живой Postgres ─────────────────────────────────────────────────────────────
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_persona_lifecycle_postgres():
    import asyncio
    import glob
    import re
    import asyncpg

    async def go():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(glob.glob("schema_v*.sql"),
                  key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cleanup():
            # диалоги/заказы переживают удаление персоны (бизнес-записи) — для
            # идемпотентности теста чистим их отдельно.
            await pool.execute("DELETE FROM bot_sales_dialogs WHERE owner_id=800777")
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=800777")
            await pool.execute("DELETE FROM bot_sales_personas WHERE owner_id=800777")

        try:
            await _cleanup()
            p = await bsp.create_persona(pool, 800777, "Анна", tone="warm",
                    operator_username="@op", handoff_triggers="возврат",
                    channels=[{"title": "K", "url": "https://t.me/x"}])
            pid = p["id"]
            await bsp.add_product(pool, pid, 800777, "Эспрессо", price_cents=999)
            assert bsp.format_price(
                (await bsp.list_products(pool, pid))[0]["price_cents"]) == "9.99 USD"
            # negative price rejected
            with pytest.raises(ValueError):
                await bsp.add_product(pool, pid, 800777, "bad", price_cents=-1)
            # assign; unique active per bot
            await bsp.assign_to_bot(pool, pid, 800777, 555777)
            assert (await bsp.get_persona_for_bot(pool, 555777))["id"] == pid
            p2 = await bsp.create_persona(pool, 800777, "Борис")
            await bsp.assign_to_bot(pool, p2["id"], 800777, 555777)   # вытесняет Анну
            assert (await bsp.get_persona_for_bot(pool, 555777))["id"] == p2["id"]
            # вернём Анну для сценария
            await bsp.assign_to_bot(pool, pid, 800777, 555777)
            # handoff
            r = await bsp.handle_incoming(pool, 555777, 800777, 10, "хочу возврат")
            assert r["handoff"] and r["operator"]["username"] == "@op"
            # order intent → draft; phone → confirmed
            r2 = await bsp.handle_incoming(pool, 555777, 800777, 11, "хочу заказать")
            assert r2["order_id"]
            await bsp.handle_incoming(pool, 555777, 800777, 11, "тел +79001234567")
            conf = [o for o in await bsp.list_orders(pool, 800777, bot_id=555777)
                    if o["status"] == "confirmed"]
            contact = conf[0]["contact"]
            if isinstance(contact, str):
                contact = json.loads(contact)
            assert conf and contact.get("phone"), "заказ не подтверждён телефоном"
            # dialog memory
            d = await bsp.get_or_create_dialog(pool, 555777, 11, 800777, pid)
            assert d["msg_count"] >= 2
            # no AI provider → fallback (не падает)
            assert await bsp.generate_reply(p, [], d, "привет")
            await _cleanup()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


# ── Интеграция в auto_responder и API (источниковые проверки) ─────────────────
def test_auto_responder_wires_sales_persona():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "services", "auto_responder.py"), encoding="utf-8").read()
    # персона вызывается в обработчике входящих и отвечает раньше шаблонных правил
    assert "bot_sales_persona" in src and "handle_incoming" in src
    assert "_notify_operator" in src            # перевод на живого оператора
    # персона обрабатывает сообщение и пропускает обычные правила (continue)
    i = src.index("handle_incoming")
    assert "континуе" not in src  # sanity
    assert "правила пропускаем" in src[i:i + 4000]  # continue-ветка персоны


def test_mini_app_api_registers_sales_routes():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "services", "mini_app_api.py"), encoding="utf-8").read()
    for route in ("/api/miniapp/sales/persona", "/api/miniapp/sales/persona/{pid}/assign",
                  "/api/miniapp/sales/persona/{pid}/product", "/api/miniapp/sales/orders"):
        assert route in src, route
