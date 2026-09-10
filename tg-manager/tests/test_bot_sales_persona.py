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


# ── Живой оператор: позиции заказа, дробление, задержка «печатает…» ────────────
def test_match_order_items_with_declension_and_qty():
    prods = [{"name": "Эспрессо", "price_cents": 999},
             {"name": "Кофемолка", "price_cents": 4500}]
    items, total = bsp.match_order_items("хочу 2 эспрессо и кофемолку", prods)
    names = {i["name"]: i["qty"] for i in items}
    assert names.get("Эспрессо") == 2            # количество рядом с названием
    assert names.get("Кофемолка") == 1           # склонение поймано стем-матчем
    assert total == 999 * 2 + 4500
    assert bsp.match_order_items("просто привет", prods) == ([], 0)


def test_match_order_items_respects_min_qty():
    # Товар с доставкой «от 2»: клиент просит 1 → количество подтягивается до 2,
    # никогда не оформляем/не считаем ниже минимума.
    prods = [{"name": "Кофе", "price_cents": 1000, "min_qty": 2, "unit": "г"}]
    items, total = bsp.match_order_items("хочу 1 кофе", prods)
    by = {i["name"]: i["qty"] for i in items}
    assert by.get("Кофе") == 2            # 1 → округлили вверх до min_qty
    assert total == 1000 * 2
    # Больше минимума — не трогаем.
    items2, _ = bsp.match_order_items("давай 5 кофе", prods)
    assert {i["name"]: i["qty"] for i in items2}.get("Кофе") == 5


def test_detect_actions_rejects_implausible_phone():
    p = {}
    # «947293» — 6 цифр, не настоящий номер → не считаем телефоном.
    assert bsp.detect_actions("мой номер 947293", p)["phone"] is None
    # 9 цифр: подходит под regex-шаблон, но телефоном НЕ является (fail-without-fix:
    # без проверки длины бот подтверждал бы заказ по такому «номеру»).
    assert bsp.detect_actions("номер 123456789", p)["phone"] is None
    # Слишком длинная цифровая мусорка (24 цифры) — тоже не телефон.
    assert bsp.detect_actions("код 123456789012345678901234", p)["phone"] is None
    # Настоящий полный номер — засчитываем.
    assert bsp.detect_actions("звоните +7 900 123-45-67", p)["phone"]
    assert bsp.detect_actions("89001234567", p)["phone"]


def test_build_prompt_min_qty_and_order_rules():
    persona = {"name": "Анна", "can_take_orders": True,
               "order_rules": "доставка от 2 г по каждой позиции"}
    products = [{"name": "Кофе", "price_cents": 1000, "currency": "USD",
                 "in_stock": True, "is_active": True, "min_qty": 2, "unit": "г"}]
    sp = bsp.build_system_prompt(persona, products)
    assert "минимальный заказ: 2 г" in sp.lower()   # минимум показан в прайсе
    assert "не предлагай" in sp.lower() or "не оформляй" in sp.lower()
    assert "доставка от 2 г по каждой позиции" in sp   # правила заказа в промпте


def test_build_prompt_forbids_inventing_name_and_meta():
    # Без имени: строгий запрет выдумывать имя (в прошлом бот придумал «Алексей»).
    sp = bsp.build_system_prompt({"name": "Анна"}, [])
    assert "НЕИЗВЕСТНО" in sp
    assert "выдумыв" in sp.lower()
    # Запрет утечки служебных пометок/рассуждений клиенту.
    assert "инструкц" in sp.lower()
    # С известным именем — используем его.
    sp2 = bsp.build_system_prompt({"name": "Анна"}, [],
                                  {"customer_name": "Пётр"})
    assert "Пётр" in sp2


def test_build_prompt_payment_modes():
    base = {"name": "Анна", "can_take_orders": True}
    # Оплата через оператора.
    sp_op = bsp.build_system_prompt({**base, "payment_via_operator": True}, [])
    assert "оплат" in sp_op.lower() and "оператор" in sp_op.lower()
    # Реквизиты.
    sp_pd = bsp.build_system_prompt({**base, "payment_details": "карта 0000 1111"}, [])
    assert "карта 0000 1111" in sp_pd
    # Ничего не задано — всё равно есть инструкция про следующий шаг оплаты.
    sp_none = bsp.build_system_prompt(base, [])
    assert "оплат" in sp_none.lower()


def test_working_hours_and_greeting():
    import datetime as dt
    # окно 09:00–18:00, будни, tz UTC+3
    p = {"work_start": "09:00", "work_end": "18:00", "work_days": "1-5", "tz_offset": 3}
    # понедельник 12:00 по UTC → 15:00 локально → рабочее
    mon_noon = dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc)  # пн
    assert bsp.is_within_hours(p, mon_noon) is True
    # понедельник 03:00 UTC → 06:00 локально → не рабочее
    assert bsp.is_within_hours(p, dt.datetime(2026, 1, 5, 3, 0, tzinfo=dt.timezone.utc)) is False
    # суббота — выходной
    assert bsp.is_within_hours(p, dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)) is False
    # без часов — всегда рабочее
    assert bsp.is_within_hours({}, mon_noon) is True
    # окно через полночь 22:00–06:00
    night = {"work_start": "22:00", "work_end": "06:00", "tz_offset": 0}
    assert bsp.is_within_hours(night, dt.datetime(2026, 1, 5, 23, 0, tzinfo=dt.timezone.utc)) is True
    assert bsp.is_within_hours(night, dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc)) is False
    # приветствие по времени
    g = {"greeting_by_time": True, "tz_offset": 0}
    assert bsp.greeting_prefix(g, dt.datetime(2026, 1, 5, 7, 0, tzinfo=dt.timezone.utc)) == "Доброе утро"
    assert bsp.greeting_prefix(g, dt.datetime(2026, 1, 5, 14, 0, tzinfo=dt.timezone.utc)) == "Добрый день"
    assert bsp.greeting_prefix(g, dt.datetime(2026, 1, 5, 20, 0, tzinfo=dt.timezone.utc)) == "Добрый вечер"
    assert bsp.greeting_prefix({"greeting_by_time": False}) == ""


def test_affirms_age():
    assert bsp._affirms_age("да, мне есть 18") is True
    assert bsp._affirms_age("мне 25") is True
    assert bsp._affirms_age("я совершеннолетний") is True
    assert bsp._affirms_age("сколько стоит кофе?") is False


def test_build_prompt_intensity_and_scope():
    soft = bsp.build_system_prompt({"name": "A", "sales_intensity": "soft"}, [])
    assert "мягк" in soft.lower()
    aggr = bsp.build_system_prompt({"name": "A", "sales_intensity": "aggressive"}, [])
    assert "актив" in aggr.lower()
    sc = bsp.build_system_prompt({"name": "A", "scope_guard": True}, [])
    assert "только по нашим" in sc.lower()


def test_build_prompt_order_thresholds_and_age():
    p = {"name": "A", "can_take_orders": True, "currency": "USD",
         "min_order_total": 1000, "free_delivery_threshold": 5000,
         "discount_max_percent": 15, "require_age_confirm": True}
    sp = bsp.build_system_prompt(p, [])
    assert "10.00 USD" in sp                     # мин. сумма заказа
    assert "50.00 USD" in sp                     # порог бесплатной доставки
    assert "15%" in sp                           # потолок скидки
    assert "18" in sp                            # возрастная проверка
    # если возраст уже подтверждён в диалоге — не переспрашиваем
    sp2 = bsp.build_system_prompt(p, [], {"age_confirmed": True})
    assert "убедись, что клиенту есть 18" not in sp2


def test_build_prompt_product_stock_variants_related():
    p = {"name": "A", "disclose_prices": True}
    products = [{"name": "Куртка", "price_cents": 5000, "currency": "USD",
                 "in_stock": True, "is_active": True, "stock_qty": 3,
                 "related_skus": "Шапка", "unit": "шт",
                 "variants": [{"name": "S", "price_cents": 5000},
                              {"name": "M", "price_cents": 5500}]}]
    sp = bsp.build_system_prompt(p, products)
    assert "в наличии: 3" in sp
    assert "варианты:" in sp and "S" in sp
    assert "с этим берут: Шапка" in sp
    # остаток 0 → нет в наличии
    products[0]["stock_qty"] = 0
    assert "нет в наличии" in bsp.build_system_prompt(p, products)


def test_merge_items():
    m, t = bsp._merge_items([{"name": "A", "qty": 1, "price_cents": 100}],
                            [{"name": "A", "qty": 3, "price_cents": 100},
                             {"name": "B", "qty": 2, "price_cents": 50}])
    by = {i["name"]: i["qty"] for i in m}
    assert by == {"A": 3, "B": 2} and t == 300 + 100


def test_split_reply_and_typing_delay():
    assert bsp.split_reply("короткий") == ["короткий"]
    # текст должен быть длиннее порога (160) И иметь пустые строки, иначе не дробим
    long = ("Первый абзац: довольно длинный текст, чтобы точно превысить порог "
            "дробления в сто шестьдесят символов и заставить функцию разбить.\n\n"
            "Второй абзац тоже с содержанием.\n\nТретий абзац.\n\nЧетвёртый абзац.")
    assert len(long) > 160
    assert len(bsp.split_reply(long, 2)) == 2
    assert 0.8 <= bsp.typing_delay("x") <= 4.0
    assert bsp.typing_delay("y" * 500) == 4.0


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_order_items_and_handoff_silence_postgres():
    import asyncio
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM bot_sales_dialogs WHERE owner_id=800888")
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=800888")
            await pool.execute("DELETE FROM bot_sales_personas WHERE owner_id=800888")
        try:
            await _cl()
            p = await bsp.create_persona(pool, 800888, "Ника", operator_username="@op",
                                         handoff_triggers="возврат")
            pid = p["id"]
            await bsp.add_product(pool, pid, 800888, "Эспрессо", price_cents=999)
            await bsp.assign_to_bot(pool, pid, 800888, 556000)
            # заказ с позицией и количеством → items/total заполнены
            await bsp.handle_incoming(pool, 556000, 800888, 70, "хочу заказать 2 эспрессо")
            await bsp.handle_incoming(pool, 556000, 800888, 70, "тел +79001112233")
            o = (await bsp.list_orders(pool, 800888, bot_id=556000))[0]
            items = o["items"]
            if isinstance(items, str):
                items = json.loads(items)
            assert items and items[0]["name"] == "Эспрессо" and items[0]["qty"] == 2
            assert o["total_cents"] == 999 * 2 and o["status"] == "confirmed"
            # хендофф → следующий ход бот молчит (оператор ведёт)
            r = await bsp.handle_incoming(pool, 556000, 800888, 71, "хочу возврат")
            assert r["handoff"] and not r.get("silent")
            r2 = await bsp.handle_incoming(pool, 556000, 800888, 71, "ну и как?")
            assert r2.get("silent") and r2.get("reply") is None
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_wave_a_settings_and_followup_postgres():
    import asyncio
    import asyncpg
    from unittest.mock import patch

    OWN, BOT, CHAT = 800999, 556999, 72
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            for t in ("bot_sales_orders", "bot_sales_dialogs",
                      "bot_sales_products", "bot_sales_personas"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
        try:
            await _cl()
            # новые поля персоны/товара реально биндятся к живому драйверу
            p = await bsp.create_persona(
                pool, OWN, "Ника", rate_limit_per_min=30, followup_enabled=True,
                followup_delay_min=1, followup_message="ещё актуально?")
            pid = p["id"]
            assert p["rate_limit_per_min"] == 30 and p["followup_enabled"] is True
            prod = await bsp.add_product(
                pool, pid, OWN, "Куртка", price_cents=5000, stock_qty=3,
                related_skus="Шапка",
                variants=[{"name": "S", "price_cents": 5000}])
            assert prod["stock_qty"] == 3
            await bsp.assign_to_bot(pool, pid, OWN, BOT)
            await pool.execute(
                "INSERT INTO managed_bots(bot_id,token,added_by,is_active) "
                "VALUES($1,$2,$3,TRUE) ON CONFLICT (bot_id) DO NOTHING",
                BOT, "tkn:" + str(BOT), OWN)

            async def _fake_gen(persona, products, dialog, text):
                return "ок"
            with patch.object(bsp, "generate_reply", _fake_gen):
                await bsp.handle_incoming(pool, BOT, OWN, CHAT, "привет", name="П")
                r2 = await bsp.handle_incoming(pool, BOT, OWN, CHAT, "ещё", name="П")
            assert r2.get("throttled") is True and r2.get("silent") is True

            # фоллоуап: диалог «замолчал» → попадает в выборку, один раз
            await pool.execute(
                "UPDATE bot_sales_dialogs SET last_at=now()-interval '5 minutes', "
                "followup_sent_at=NULL WHERE bot_id=$1", BOT)
            due = await bsp.list_followup_due(pool)
            assert due and due[0]["customer_chat_id"] == CHAT
            assert due[0]["followup_message"] == "ещё актуально?"
            await bsp.mark_followup_sent(pool, due[0]["dialog_id"])
            assert await bsp.list_followup_due(pool) == []   # антидубль
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


def test_auto_responder_human_delivery_and_silence():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "services", "auto_responder.py"), encoding="utf-8").read()
    assert "_deliver_sales_reply" in src        # доставка «по-человечески»
    assert "sendChatAction" in src              # печатает…
    assert 'if _res.get("silent")' in src       # молчит после хендоффа


def test_diagnose_no_providers(monkeypatch):
    import asyncio
    from services import ai_providers
    monkeypatch.setattr(ai_providers, "configured_providers", lambda: [])
    r = asyncio.new_event_loop().run_until_complete(
        bsp.diagnose_generation({"name": "A"}, []))
    assert r["ok"] is False and r["providers"] == []
    assert "провайдер" in r["error"].lower()


def test_diagnose_reports_provider_error(monkeypatch):
    import asyncio
    from services import ai_providers
    fake = ai_providers.AiProvider(name="openai", api_key="k",
                                   base_url="http://x", models=["m"])
    monkeypatch.setattr(ai_providers, "configured_providers", lambda: [fake])
    r = asyncio.new_event_loop().run_until_complete(
        bsp.diagnose_generation({"name": "A"}, []))
    # openai не установлен в тест-среде ИЛИ вызов упадёт — в любом случае error непустой,
    # провайдер виден в списке (на неудаче provider=None — успеха не было)
    assert r["ok"] is False and "openai" in r["providers"] and r["error"]
