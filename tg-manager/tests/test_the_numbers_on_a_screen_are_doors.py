"""Три экрана были числами без дверей — обход 06.11.2026.

Жалоба владельца (28.09.2026): «экраны, которые только отображают числа, и в
них всё не кликабельно». Обход мини-аппа нашёл последние три таких:

* «Аудитория» бота — пять чисел, диаграмма роста и теги с `cursor:default`.
  Числа здесь — это люди, но посмотреть их было нельзя, а написать им тем
  более: движок рассылок умеет и всех подписчиков, и когорты по активности с
  самого начала.
* «Топики ноды» — список веток: номер показан, а перейти в саму переписку
  нельзя; карточку аккаунта из его топика — тоже.
* «Аккаунт в инвайте» — разбор лимита без выхода: ни к самому аккаунту
  (прогрев, прокси, статус), ни даже к повтору после сбоя.

Проверки держат именно двери. Срез подписчиков по тегу — ещё и на сервере:
его видно по фактическому ответу ручки, а не только по тексту клиента.
"""
from __future__ import annotations

import asyncio

from tests.miniapp_source import api_function, js_function

# ── Аудитория бота ──────────────────────────────────────────────────────────


def test_the_audience_numbers_lead_where_the_people_are():
    body = js_function("buildAudience")
    assert "openBotSubs(" in body, "«Всего» не ведёт к списку подписчиков"
    assert "openEngagement(" in body, "«Активных» не ведёт к когортам"
    assert "openNewUsers(" in body, "числа новых подписчиков никуда не ведут"
    assert 'style="cursor:default"' not in body, "в экране остались тупики-строки"


def test_the_subscriber_tag_opens_its_people():
    body = js_function("buildAudience")
    assert "openBotSubs(${botId}" in body and "escJs(t.tag" in body, (
        "тег подписчика остался числом: кто эти люди — не посмотреть")


def test_writing_to_subscribers_is_a_door_of_its_own():
    """Рассылку нельзя приписывать числу «за 30 дней»: это другие люди."""
    assert "openBcastForBot(AUD_BOT" in js_function("bcastFromAudience")
    assert "AUD_ACTIVE = Number(d.total)" in js_function("loadAudience"), (
        "кнопка рассылки обещала бы получателей, которых не посчитали")


def test_a_failed_audience_retries_the_load_not_the_open():
    """Открывашка сбрасывает состояние экрана, а повтору нужны только данные.

    (В стек повтор того же верхнего id ничего не кладёт — `push()` это уже
    проверяет сам; старые комментарии в коде утверждали обратное.)
    """
    assert "loadAudience(" in js_function("loadAudience")
    assert "push(" not in js_function("loadAudience")
    assert "push('s-audience')" in js_function("openAudience")


# ── Список подписчиков ──────────────────────────────────────────────────────


def test_a_subscriber_row_is_a_door():
    body = js_function("loadBotSubs")
    assert "openSubscriberCard(" in body, "строка подписчика — снова тупик"
    assert 'style="cursor:default"' not in body


def test_the_tag_slice_asks_the_server():
    body = js_function("loadBotSubs")
    assert "tag='+encodeURIComponent(BS_TAG)" in body, (
        "срез по тегу фильтровался бы клиентом по одной странице из пятидесяти")


def test_the_tag_slice_is_scoped_to_the_owners_bot():
    body = api_function("bot_subscribers")
    assert "_user_can_use_bot" in body, "срез по тегу без проверки владельца"
    assert "user_tags" in body and "ut.tag=$2" in body
    assert "EXISTS (SELECT 1 FROM user_tags" in body, (
        "срез через JOIN удвоил бы человека, у которого тег стоит дважды")
    assert body.count("_safe_fetch") == 1, (
        "две выборки на один ключ — и сторож контракта перестаёт его сверять "
        "(tests/test_api_contract_row_fields.py)")


# ── Топики ноды ─────────────────────────────────────────────────────────────


class _NodePool:
    """Нода с одним топиком: chat_id задаёт тест."""

    def __init__(self, chat_id: int, thread_id: int | None = 5):
        self.chat_id = chat_id
        self.thread_id = thread_id

    async def fetchrow(self, sql, *a):
        if "bm_telegram_nodes" in sql:
            return {"id": 3, "name": "Нода", "tg_chat_id": self.chat_id}
        return None

    async def fetch(self, sql, *a):
        if "bm_node_threads" in sql:
            return [{"id": 1, "tg_thread_id": self.thread_id, "entity_type": "account",
                     "entity_id": 42, "topic_name": "Аккаунт 42", "status": "open",
                     "created_at": None}]
        return []

    async def fetchval(self, *a, **k): return 0

    async def execute(self, *a, **k): return "OK"


def _threads(monkeypatch, pool):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/miniapp/node/3/threads")
            return r.status, await r.json()
        finally:
            await client.close()

    return asyncio.run(_go())


def test_a_node_topic_carries_a_link_to_its_thread(monkeypatch):
    status, d = _threads(monkeypatch, _NodePool(-1001234567890))
    assert status == 200
    assert d["threads"][0]["tg_link"] == "https://t.me/c/1234567890/5"


def test_a_topic_without_a_supergroup_promises_no_link(monkeypatch):
    """В обычном чате тем нет: ссылка на ветку вела бы в никуда."""
    status, d = _threads(monkeypatch, _NodePool(777000))
    assert status == 200 and d["threads"][0]["tg_link"] == ""
    status, d = _threads(monkeypatch, _NodePool(-1001234567890, thread_id=None))
    assert d["threads"][0]["tg_link"] == ""


def test_the_topic_row_opens_the_thread_and_the_account():
    body = js_function("ntRender")
    assert "openTgLink(" in body, "строка топика осталась текстом"
    assert "openAccount(" in body, "из топика аккаунта нет двери в его карточку"
    assert "event.stopPropagation()" in body, (
        "кнопка карточки открывала бы заодно и ветку")
    assert "ntSetFilter(" in body, "закрытые топики не спрятать, а их со временем больше"


def test_a_topic_list_without_a_link_says_so_instead_of_pretending():
    body = js_function("ntRender")
    assert "t.tg_link ?" in body and "toast(" in body, (
        "строка без номера ветки молча ничего не делала бы")


# ── Аккаунт в инвайте ───────────────────────────────────────────────────────


def test_the_invite_account_card_has_a_door_to_the_account():
    body = js_function("loadInviteAccount")
    assert "openAccount(" in body, "разбор лимита остался без выхода к аккаунту"
    assert "errHtml(" in body and "loadInviteAccount(" in body, (
        "после сбоя карточку нельзя было даже перезапросить")


def test_the_invite_account_retry_does_not_stack_the_screen():
    assert "push('s-invacc')" in js_function("openInviteAccount")
    assert "push(" not in js_function("loadInviteAccount")

# ── Результаты A/B-теста ────────────────────────────────────────────────────


def test_the_ab_variant_row_opens_its_operation():
    """Доставка варианта — это отправки, и карточка операции уже написана."""
    body = js_function("openAbResults")
    assert "abOpenVariant(" in body, "строка варианта осталась процентами"
    assert "v.ops" in body, "строка не знает, какие отправки за ней стоят"
    door = js_function("abOpenVariant")
    assert "openOpDetail(" in door, "дверь ведёт не в карточку операции"
    assert "toast(" in door, (
        "у варианта может быть несколько отправок — молчаливый выбор первой "
        "читается как потерянные")


def test_the_server_names_the_operations_behind_each_variant():
    body = api_function("ab_results")
    assert "ARRAY_AGG(id ORDER BY id) AS ops" in body, (
        "без id отправок дверь пришлось бы искать клиенту по всей очереди")
    assert '"ops": [int(x) for x in (r["ops"] or [])][:20]' in body, (
        "список отправок без границы — на экран уедет вся пачка")

class _AbPool:
    """Один батч A/B с двумя вариантами; у второго отправок больше двадцати."""

    async def fetchval(self, sql, *a):
        return 7 if "ab_batch" in sql else 0

    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, sql, *a):
        if "ab_variant" in sql:
            return [{"variant": "Вариант А", "sent": 500, "delivered": 40,
                     "ops": [11]},
                    {"variant": "Вариант Б", "sent": 500, "delivered": 65,
                     "ops": list(range(100, 125))}]
        return []

    async def execute(self, *a, **k):
        return "OK"


def _ab_results(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, _AbPool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/miniapp/ab/results")
            return r.status, await r.json()
        finally:
            await client.close()

    return asyncio.run(_go())


def test_the_ab_variants_carry_their_operations(monkeypatch):
    status, d = _ab_results(monkeypatch)
    assert status == 200
    assert d["variants"][0]["ops"] == [11]
    assert len(d["variants"][1]["ops"]) == 20, (
        "двадцать пять отправок уехали на экран целиком")
    assert d["variants"][1]["ops"][0] == 100
