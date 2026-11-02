"""Сценарий исполняется по-настоящему — на живой базе, шаг за шагом.

ЧТО БЫЛО. Конструктор сценариев сохранял шаги, а «запуск» заводил строку
`workflow_runs` со статусом running, которую больше не трогал никто: шаги не
выполнялись, по событию сценарий не запускался, кнопок-веток и вопросов не
было вовсе.

ЧТО ПРОВЕРЯЕМ. Полный путь исполнителя (`services/workflow_runtime.py`)
против настоящего Postgres: событие шины → запуск по триггеру → сообщение с
кнопками → нажатие → вопрос с проверкой ответа → поле контакта → условие →
тег → завершение. Плюс то, что ломается в исполнителях первым: двойная
отправка при двух исполнителях, повторный вход, тайм-аут ожидания, «стоп»,
оплата счёта и повтор апдейта оплаты.

Telegram подменён: отправки складываются в список, база — настоящая.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993771
BOT_ID = 880077123
USER = 70771

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p):
    await p.execute("DELETE FROM workflow_step_log WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM crm_deals WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM keyword_hits WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM keyword_watchers WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM workflow_runs WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM workflow_definitions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM automation_events WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM user_tags WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM bot_sales_orders WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM bot_sales_personas WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=6)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture()
def tg(pool, monkeypatch):
    """Подменённый Telegram: всё, что исполнитель отправил, — в `sent`."""
    from services import bot_api, brand_injection, workflow_runtime as rt

    sent: list[dict] = []

    async def _send(http, token, chat_id, text, buttons=None, reply_markup=None,
                    disable_notification=False):
        sent.append({"method": "sendMessage", "chat_id": chat_id, "text": text,
                     "reply_markup": reply_markup})
        return True, None, ""

    async def _call(http, token, method, **params):
        sent.append({"method": method, **params})
        return {"ok": True, "result": {}}

    async def _free(pool, bot_id):
        return False

    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    monkeypatch.setattr(bot_api, "_call", _call)
    monkeypatch.setattr(brand_injection, "is_free_tier", _free)

    async def _fresh():
        await _cleanup(pool)
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'wf_runtime_test_bot',TRUE,$3)",
            f"1234:wf-runtime-{BOT_ID}", BOT_ID, OWNER)
        await pool.execute(
            "INSERT INTO bot_users(bot_id, user_id, first_name, is_active) "
            "VALUES($1,$2,'Аня',TRUE)", BOT_ID, USER)
        # Курсор исполнителя — на конец шины, как у живого процесса.
        await pool.execute("DELETE FROM automation_cursors WHERE consumer=$1",
                           rt.CONSUMER)
        await rt.bus.get_cursor(pool, rt.CONSUMER)

    _run(_fresh())
    return sent


async def _workflow(pool, steps, trigger="start", value=None, active=True,
                    reentry=False):
    from services import workflow_engine as wf
    steps = wf.validate_steps(steps)
    res = await wf.create_workflow(pool, OWNER, "Тест", steps=steps,
                                   is_active=active, bot_id=BOT_ID)
    await wf.set_trigger(pool, OWNER, res["id"], trigger, value, reentry)
    return res["id"]


async def _emit_old(pool, event_type, data=None):
    """Событие шины, «отстоявшееся» две секунды (читатель свежие не берёт)."""
    from services import automation_events as bus
    eid = await bus.emit(pool, OWNER, event_type, data or {}, bot_id=BOT_ID,
                         chat_id=USER)
    await pool.execute("UPDATE automation_events SET created_at = now() - "
                       "interval '5 seconds' WHERE id=$1", eid)
    return eid


async def _tick(pool):
    import aiohttp
    from services import workflow_runtime as rt
    async with aiohttp.ClientSession() as http:
        await rt.tick(pool, http, None)


async def _run_row(pool, wf_id):
    return await pool.fetchrow(
        "SELECT * FROM workflow_runs WHERE workflow_id=$1 ORDER BY id DESC LIMIT 1",
        wf_id)


def _texts(sent):
    return [m["text"] for m in sent if m["method"] == "sendMessage"]


# ── Полный путь ──────────────────────────────────────────────────────────────

def test_a_start_runs_the_whole_branching_scenario(pool, tg):
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "message", "text": "Что интересует?",
             "buttons": [{"text": "Купить", "goto": 2}, {"text": "Смотрю", "goto": 6}]},
            {"type": "ask", "text": "Ваш телефон?", "save_as": "phone",
             "validate": "phone"},
            {"type": "condition", "condition": "field_set", "condition_value": "phone",
             "else_goto": 6},
            {"type": "action", "action": "add_tag", "value": "покупатель"},
            {"type": "action", "action": "stop"},
            {"type": "message", "text": "Ветка «смотрю» — сюда не попадём"},
        ])
        await _emit_old(pool, "contact.started", {"param": ""})
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run is not None, "событие /start не запустило сценарий"
        assert run["status"] == "waiting" and run["wait_kind"] == "button"
        kb = next(m for m in tg if m["method"] == "sendMessage")["reply_markup"]
        cb = kb["inline_keyboard"][0][0]["callback_data"]
        assert cb.startswith("wf:")

        # Нажатие «Купить» → вопрос.
        handled = await rt.on_button(pool, None, "tok", BOT_ID, USER,
                                     {"data": cb, "message": {"message_id": 5}})
        assert handled
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "waiting" and run["wait_kind"] == "reply"
        assert "Ваш телефон?" in _texts(tg)

        # Неверный ответ — подсказка, прогон ждёт дальше.
        r = await rt.on_reply(pool, BOT_ID, USER, "не скажу")
        assert r["consumed"] and r["reply"], r
        assert (await _run_row(pool, wf_id))["status"] == "waiting"
        # Верный — поле в карточке, сценарий идёт дальше.
        r = await rt.on_reply(pool, BOT_ID, USER, "+7 900 123-45-67")
        assert r == {"consumed": True, "reply": None}
        fields = await pool.fetchval(
            "SELECT fields FROM bot_users WHERE bot_id=$1 AND user_id=$2", BOT_ID, USER)
        fields = json.loads(fields) if isinstance(fields, str) else fields
        assert fields.get("phone") == "+79001234567"

        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "completed", dict(run)
        tags = await pool.fetch("SELECT tag FROM user_tags WHERE bot_id=$1 AND user_id=$2",
                                BOT_ID, USER)
        assert [t["tag"] for t in tags] == ["покупатель"]
        assert "Ветка «смотрю» — сюда не попадём" not in _texts(tg)
        kinds = {r["event_type"] for r in await pool.fetch(
            "SELECT event_type FROM automation_events WHERE owner_id=$1", OWNER)}
        assert {"button.clicked", "contact.field_set", "contact.tag_added",
                "workflow.completed"} <= kinds, kinds

    _run(go())


def test_two_executors_do_not_send_one_message_twice(pool, tg):
    """Две реплики забрали один прогон — сообщение уходит один раз."""
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [{"type": "message", "text": "Один раз"}],
                                trigger="manual")
        wf = dict(await pool.fetchrow("SELECT * FROM workflow_definitions WHERE id=$1",
                                      wf_id))
        await rt.start_run(pool, wf, BOT_ID, USER, force=True)
        row = dict(await _run_row(pool, wf_id))
        await asyncio.gather(rt.advance(pool, None, dict(row)),
                             rt.advance(pool, None, dict(row)))
        assert _texts(tg).count("Один раз") == 1, _texts(tg)

    _run(go())


def test_a_person_passes_a_scenario_once_unless_reentry_is_on(pool, tg):
    async def go():
        wf_id = await _workflow(pool, [{"type": "message", "text": "Привет"}])
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        n = await pool.fetchval("SELECT COUNT(*) FROM workflow_runs WHERE workflow_id=$1",
                                wf_id)
        assert n == 1, "повторный /start прогнал приветствие заново"
        assert _texts(tg).count("Привет") == 1

    _run(go())


def test_a_paused_or_foreign_bot_workflow_does_not_start(pool, tg):
    async def go():
        wf_id = await _workflow(pool, [{"type": "message", "text": "Нет"}], active=False)
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        assert await _run_row(pool, wf_id) is None, "выключенный сценарий запустился"

    _run(go())


def test_history_is_not_replayed_for_a_new_reader(pool, tg):
    """Первый запуск исполнителя не отыгрывает накопленные события: иначе
    приветствие получили бы все, кто когда-либо открывал бота."""
    from services import automation_events as bus

    async def go():
        await pool.execute("DELETE FROM automation_cursors WHERE consumer='fresh-reader'")
        old = await _emit_old(pool, "contact.started")
        cur = await bus.get_cursor(pool, "fresh-reader")
        assert cur >= old
        assert not [e for e in await bus.read_since(pool, cur) if e["id"] == old]

    _run(go())


def test_an_unanswered_question_expires_or_follows_its_timeout_branch(pool, tg):
    async def go():
        wf_id = await _workflow(pool, [
            {"type": "ask", "text": "Имя?", "save_as": "name", "timeout_goto": 2},
            {"type": "message", "text": "Не дождались ответа"},
        ])
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        await pool.execute("UPDATE workflow_runs SET wait_until=now()-interval '1 minute' "
                           "WHERE workflow_id=$1", wf_id)
        await _tick(pool)
        await _tick(pool)
        assert "Не дождались ответа" in _texts(tg)
        assert (await _run_row(pool, wf_id))["status"] == "completed"

    _run(go())


def test_stop_word_cancels_the_scenario(pool, tg):
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "message", "text": "Раз"},
            {"type": "delay", "delay_minutes": 60},
            {"type": "message", "text": "Два"},
        ])
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        assert await rt.cancel_for_contact(pool, BOT_ID, USER) == 1
        await pool.execute("UPDATE workflow_runs SET next_run_at=now() WHERE workflow_id=$1",
                           wf_id)
        await _tick(pool)
        assert "Два" not in _texts(tg)
        assert (await _run_row(pool, wf_id))["status"] == "cancelled"

    _run(go())


def test_an_invoice_waits_for_payment_and_continues_once(pool, tg):
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "invoice", "title": "Консультация", "amount": 50, "currency": "XTR"},
            {"type": "condition", "condition": "paid"},
            {"type": "message", "text": "Спасибо за оплату"},
        ])
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        inv = next(m for m in tg if m["method"] == "sendInvoice")
        assert inv["currency"] == "XTR" and inv["provider_token"] == ""
        assert inv["prices"][0]["amount"] == 50
        ok, err = await rt.check_pre_checkout(
            pool, BOT_ID, {"invoice_payload": inv["payload"], "from": {"id": USER}})
        assert ok and err is None
        pay = {"invoice_payload": inv["payload"], "total_amount": 50, "currency": "XTR",
               "telegram_payment_charge_id": "ch-1"}
        assert await rt.on_payment(pool, BOT_ID, USER, pay)
        await _tick(pool)
        assert _texts(tg).count("Спасибо за оплату") == 1
        # Повтор апдейта оплаты не двигает завершённый прогон дальше.
        await rt.on_payment(pool, BOT_ID, USER, pay)
        await _tick(pool)
        assert _texts(tg).count("Спасибо за оплату") == 1
        assert (await _run_row(pool, wf_id))["status"] == "completed"
        # По завершённому прогону деньги больше не берутся.
        ok, err = await rt.check_pre_checkout(
            pool, BOT_ID, {"invoice_payload": inv["payload"], "from": {"id": USER}})
        assert err, "счёт закрытого прогона принят к оплате"

    _run(go())


def test_a_stale_button_does_not_move_the_run(pool, tg):
    """Кнопка из старого сообщения (счётчик другой) не ведёт никуда."""
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "message", "text": "Выбор", "buttons": [{"text": "А"}]},
            {"type": "message", "text": "После А"},
        ])
        await _emit_old(pool, "contact.started")
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        stale = f"wf:{run['id']}:{run['exec_seq'] + 5}:0"
        assert await rt.on_button(pool, None, "tok", BOT_ID, USER, {"data": stale})
        assert (await _run_row(pool, wf_id))["status"] == "waiting"

    _run(go())


# ── Оплата заказа ИИ-продавца ────────────────────────────────────────────────

def test_an_order_is_paid_once_and_a_tampered_amount_is_refused(pool, tg):
    from services import bot_sales_persona as bsp

    async def go():
        persona = await bsp.create_persona(pool, OWNER, "Аня",
                                           payment_in_chat="stars", stars_per_unit=50,
                                           currency="USD")
        await pool.execute("UPDATE bot_sales_personas SET bot_id=$1, is_active=TRUE "
                           "WHERE id=$2", BOT_ID, persona["id"])
        order = await bsp.create_order(pool, OWNER, BOT_ID, persona["id"], USER,
                                       total_cents=1990, currency="USD")
        await bsp.update_order(pool, order["id"], OWNER, status="confirmed")
        assert await bsp.send_order_invoice(pool, None, "tok", BOT_ID, USER, order["id"])
        inv = next(m for m in tg if m["method"] == "sendInvoice")
        # 19.90 $ × 50 звёзд = 995 звёзд; округление вверх, не вниз.
        assert inv["currency"] == "XTR" and inv["prices"][0]["amount"] == 995
        q = {"invoice_payload": inv["payload"], "from": {"id": USER},
             "currency": "XTR", "total_amount": 995}
        assert await bsp.check_order_pre_checkout(pool, BOT_ID, q) == (True, None)
        bad = dict(q, total_amount=1)
        assert (await bsp.check_order_pre_checkout(pool, BOT_ID, bad))[1]
        pay = {"invoice_payload": inv["payload"], "total_amount": 995, "currency": "XTR",
               "telegram_payment_charge_id": "ord-ch-1"}
        assert await bsp.mark_order_paid(pool, None, "tok", BOT_ID, USER, pay)
        assert not await bsp.mark_order_paid(pool, None, "tok", BOT_ID, USER, pay), (
            "повтор апдейта оплаты задвоил оплату")
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM automation_events WHERE owner_id=$1 "
            "AND event_type='payment.received'", OWNER)
        assert n == 1
        assert (await bsp.check_order_pre_checkout(pool, BOT_ID, q))[1], (
            "оплаченный заказ можно оплатить второй раз")
        # Токен провайдера не уходит наружу.
        p = await bsp.update_persona(pool, persona["id"], OWNER,
                                     payment_provider_token="123:LIVE:secret")
        assert "payment_provider_token" not in p and p["payment_provider_set"] is True
        raw = await pool.fetchval("SELECT payment_provider_token FROM bot_sales_personas "
                                  "WHERE id=$1", persona["id"])
        assert raw and "secret" not in raw, "токен провайдера лежит в базе открытым"

    _run(go())


# ── Цель, журнал, выручка, источник, сделка, A/B, умная задержка ────────────

def test_reaching_the_goal_stops_the_follow_up(pool, tg):
    """Человек оплатил (или получил тег-цель) — дожим больше не идёт.

    Раньше сценарий не знал цели и слал «напоминаем об оплате» тому, кто уже
    оплатил."""
    from services import workflow_engine as wf, workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "message", "text": "Привет"},
            {"type": "delay", "delay_minutes": 60},
            {"type": "message", "text": "Напоминаем об оплате"},
        ])
        await wf.set_goal(pool, OWNER, wf_id, "tag_added", "клиент")
        await _emit_old(pool, "contact.started", {"param": ""})
        await _tick(pool)
        assert (await _run_row(pool, wf_id))["status"] == "running"  # ждёт паузу
        await _emit_old(pool, "contact.tag_added", {"tag": "Клиент"})
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "completed" and run["goal_reached_at"] is not None
        # Пауза истекла бы — но прогон закрыт, напоминание не уходит.
        await pool.execute("UPDATE workflow_runs SET next_run_at=now() WHERE id=$1",
                           run["id"])
        await _tick(pool)
        assert "Напоминаем об оплате" not in _texts(tg)
        stats = await wf.workflow_stats(pool, OWNER, wf_id, "tag_added")
        assert stats["runs"]["goal"] == 1 and stats["conversion"] == "goal"

    _run(go())


def test_the_journal_counts_people_per_step_and_revenue_once(pool, tg):
    """Журнал шагов: сколько людей на каждом шаге, кто что нажал, сколько
    денег. Повтор апдейта оплаты не удваивает выручку."""
    from services import workflow_engine as wf, workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "message", "text": "Берёте?",
             "buttons": [{"text": "Да", "goto": 2}, {"text": "Нет", "goto": 3}]},
            {"type": "invoice", "title": "Курс", "amount": 70, "currency": "XTR"},
            {"type": "message", "text": "Конец"},
        ])
        await _emit_old(pool, "contact.started", {"param": "promo_tt"})
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["source"] == "promo_tt", "параметр ссылки не стал источником прогона"
        kb = next(m for m in tg if m["method"] == "sendMessage")["reply_markup"]
        cb = kb["inline_keyboard"][0][0]["callback_data"]
        await rt.on_button(pool, None, None, BOT_ID, USER, {"data": cb})
        await _tick(pool)
        inv = next(m for m in tg if m["method"] == "sendInvoice")
        pay = {"invoice_payload": inv["payload"], "total_amount": 70,
               "currency": "XTR", "telegram_payment_charge_id": "ch-journal"}
        await rt.on_payment(pool, BOT_ID, USER, pay)
        await rt.on_payment(pool, BOT_ID, USER, pay)  # повтор апдейта
        await _tick(pool)
        stats = await wf.workflow_stats(pool, OWNER, wf_id)
        assert stats["steps"]["0"]["outcomes"].get("button:0") == 1
        assert stats["steps"]["1"]["outcomes"].get("paid") == 1
        assert stats["revenue"] == [{"currency": "XTR", "amount": 70, "payments": 1}]
        assert stats["sources"][0]["source"] == "promo_tt"
        assert stats["sources"][0]["paid"] == 1
        payments = await pool.fetchval(
            "SELECT count(*) FROM automation_events WHERE owner_id=$1 "
            "AND event_type='payment.received'", OWNER)
        assert payments == 1, "повтор апдейта оплаты записал второе событие"
        journal = await wf.run_log(pool, OWNER, run["id"])
        outcomes = [r["outcome"] for r in journal["log"]]
        assert outcomes[:3] == ["sent", "button:0", "sent"] and "paid" in outcomes
        assert await wf.run_log(pool, OWNER + 1, run["id"]) is None  # чужой

    _run(go())


def test_a_deal_from_a_scenario_is_created_and_won_by_payment(pool, tg):
    """Регресс: «Создать сделку» писала этап 'new', которого не пускает
    ограничение crm_deals — сделка не создавалась. Оплата счёта прогона
    переводит его сделку в «выиграна» и пишет deal.stage_changed."""
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "action", "action": "create_deal", "value": "Заявка"},
            {"type": "invoice", "title": "Курс", "amount": 10, "currency": "XTR"},
        ])
        await _emit_old(pool, "contact.started", {"param": ""})
        await _tick(pool)
        deal = await pool.fetchrow("SELECT * FROM crm_deals WHERE owner_id=$1", OWNER)
        assert deal is not None, "сделка из сценария не создана"
        assert deal["stage"] == "lead"
        inv = next(m for m in tg if m["method"] == "sendInvoice")
        await rt.on_payment(pool, BOT_ID, USER, {
            "invoice_payload": inv["payload"], "total_amount": 10, "currency": "XTR",
            "telegram_payment_charge_id": "ch-deal"})
        assert (await pool.fetchval("SELECT stage FROM crm_deals WHERE id=$1",
                                    deal["id"])) == "won"
        ev = await pool.fetchrow(
            "SELECT data FROM automation_events WHERE owner_id=$1 "
            "AND event_type='deal.stage_changed'", OWNER)
        data = json.loads(ev["data"]) if isinstance(ev["data"], str) else ev["data"]
        assert data["from"] == "lead" and data["to"] == "won"

    _run(go())


def test_tags_removed_anywhere_and_keyword_hits_reach_the_bus(pool, tg):
    """Снятие тега любым путём и находка ловца слов — события шины."""
    from database import db
    from services import keyword_watcher as kw

    async def go():
        await db.add_user_tag(pool, BOT_ID, USER, "лид")
        await db.remove_user_tag(pool, BOT_ID, USER, "лид")
        await db.remove_user_tag(pool, BOT_ID, USER, "лид")  # тега уже нет
        n = await pool.fetchval(
            "SELECT count(*) FROM automation_events WHERE owner_id=$1 "
            "AND event_type='contact.tag_removed'", OWNER)
        assert n == 1
        wid = await pool.fetchval(
            "INSERT INTO keyword_watchers(owner_id, account_id, chat_ref, keywords) "
            "VALUES($1, 1, '@chat', '[\"куплю\"]'::jsonb) RETURNING id", OWNER)
        watcher = dict(await pool.fetchrow("SELECT * FROM keyword_watchers WHERE id=$1", wid))

        async def fetch(pool_, w, cursor, limit):
            return [{"message_id": 5, "text": "Куплю бота", "from_user_id": 42,
                     "from_username": "buyer"}]

        async def deliver(*a, **k):
            return True

        kw_deliver = kw._deliver_hit
        kw._deliver_hit = deliver
        try:
            await kw.poll_watcher(pool, None, watcher, fetch=fetch)
        finally:
            kw._deliver_hit = kw_deliver
        ev = await pool.fetchrow(
            "SELECT data FROM automation_events WHERE owner_id=$1 "
            "AND event_type='keyword.hit'", OWNER)
        assert ev is not None, "находка ловца не попала в шину"

    _run(go())


def test_split_sends_people_down_weighted_branches(pool, tg):
    from services import workflow_engine as wf

    async def go():
        wf_id = await _workflow(pool, [
            {"type": "split", "branches": [{"label": "A", "weight": 100, "goto": 2},
                                           {"label": "B", "weight": 1, "goto": 3}]},
            {"type": "message", "text": "Ветка A"},
            {"type": "message", "text": "Ветка B"},
        ])
        await _emit_old(pool, "contact.started", {"param": ""})
        await _tick(pool)
        sent = _texts(tg)
        run = await _run_row(pool, wf_id)
        assert sent and sent[0] in ("Ветка A", "Ветка B"), {k: run[k] for k in ('status','current_step','exec_seq','fail_count','error_message','next_run_at','vars')} if run else None
        stats = await wf.workflow_stats(pool, OWNER, wf_id)
        sp = stats["splits"]["0"]
        assert sum(b["sent"] for b in sp["branches"]) == 1

    _run(go())


def test_smart_delay_waits_for_the_persons_weekday_morning():
    """«До 10:00 в будни по времени человека»: пятница вечер по Москве →
    понедельник 10:00 по Москве (07:00 UTC)."""
    from datetime import datetime, timezone
    from services import workflow_runtime as rt

    fri_evening = datetime(2026, 10, 30, 17, 0, tzinfo=timezone.utc)  # 20:00 МСК
    when = rt.next_window(fri_evening, "10:00", True, rt.contact_tz({}, "ru"))
    assert when == datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)
    # Без «только будни» — завтра, в субботу.
    when = rt.next_window(fri_evening, "10:00", False, "Europe/Moscow")
    assert when == datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc)
    # Поле контакта timezone сильнее языка.
    assert rt.contact_tz({"timezone": "Asia/Almaty"}, "ru") == "Asia/Almaty"
    assert rt.contact_tz({}, "xx") == rt.DEFAULT_TZ
