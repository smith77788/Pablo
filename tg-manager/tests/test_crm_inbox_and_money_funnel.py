"""Единый инбокс, путь клиента в карточке и воронка денег — чистая логика.

Что стережёт:

* инбокс сводит ОДНОГО человека из четырёх источников в одну строку и
  правильно решает «ждёт ответа» по последнему слову разговора;
* карточка отвечает на четыре вопроса (откуда пришёл, что получил, что
  ответил, что купил), и отменённый заказ покупкой не считается;
* воронка приписывает событие последней кампании В ОКНЕ, а событие до
  касания или после окна — «Без рассылки»; стоимость лида = расход /
  ответившие; сравнение кампаний не объявляет значимой разницу на малой
  выборке;
* слушатель аккаунтов пишет личное входящее в инбокс, а не только сенсору.

Запросы к базе проверяет tests/test_crm_inbox_and_money_funnel_postgres.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services import money_funnel as mf
from services import unified_inbox as ui
from services.contacts_hub import timeline as tl

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _h(n):
    return T0 + timedelta(hours=n)


# ── Инбокс ────────────────────────────────────────────────────────────────

def test_one_person_from_four_sources_is_one_thread():
    th = ui.merge_threads(
        fleet=[{"peer_user_id": 5, "peer_name": "Аня", "account_id": 11,
                "last_at": _h(1), "last_text": "привет", "unread": 1, "total": 1}],
        vault=[{"peer_user_id": 5, "last_at": _h(2), "last_text": "ещё",
                "last_direction": "out", "total": 3}],
        bots=[{"peer_user_id": 5, "bot_id": 1, "bot_username": "shop_bot",
               "last_at": _h(0), "last_text": "хочу", "last_role": "user", "total": 2}],
        relay=[{"peer_user_id": 5, "bot_id": 2, "bot_username": "help_bot",
                "last_at": _h(3), "last_text": "сколько стоит?",
                "last_direction": "in", "total": 1}],
        contacts={5: "c-uuid"})
    assert len(th) == 1
    t = th[0]
    assert t["name"] == "Аня"
    assert t["contact_id"] == "c-uuid"
    assert t["total"] == 7 and t["unread"] == 1
    assert {c["source"] for c in t["channels"]} == {"fleet", "vault", "bot", "relay"}
    # Последнее слово — входящее в релей: человек ждёт ответа.
    assert t["last_text"] == "сколько стоит?" and t["waiting"] is True


def test_waiting_follows_the_last_word():
    th = ui.merge_threads(
        vault=[{"peer_user_id": 7, "last_at": _h(5), "last_text": "ответили ему",
                "last_direction": "out", "total": 2}])
    assert th[0]["waiting"] is False
    assert ui.merge_threads(vault=[{"peer_user_id": 7, "last_at": _h(5),
                                    "last_direction": "out"}], only_waiting=True) == []


def test_waiting_threads_go_first_then_fresh():
    th = ui.merge_threads(
        vault=[{"peer_user_id": 1, "last_at": _h(9), "last_direction": "out"},
               {"peer_user_id": 2, "last_at": _h(1), "last_direction": "in"},
               {"peer_user_id": 3, "last_at": _h(4), "last_direction": "in"}])
    assert [t["peer_user_id"] for t in th] == [3, 2, 1]


def test_handoff_bot_dialog_waits_for_a_human():
    th = ui.merge_threads(bots=[{"peer_user_id": 4, "last_at": _h(1),
                                 "last_role": "assistant", "handed_off": True}])
    assert th[0]["waiting"] is True


def test_messages_are_one_chronological_feed():
    msgs = ui.merge_messages(
        fleet=[{"msg_date": _h(2), "text": "b", "account_id": 3}],
        vault=[{"msg_date": _h(1), "text": "a", "direction": "in"}],
        relay=[{"created_at": _h(3), "text": "c", "direction": "out",
                "bot_username": "x_bot"}])
    assert [m["text"] for m in msgs] == ["a", "b", "c"]
    assert msgs[-1]["direction"] == "out" and msgs[-1]["channel"] == "Релей @x_bot"


class _Pool:
    def __init__(self):
        self.rows = set()

    async def execute(self, q, *a):
        key = (a[1], a[2], a[5])
        if key in self.rows:
            return "INSERT 0 0"
        self.rows.add(key)
        return "INSERT 0 1"


def test_record_incoming_is_idempotent(monkeypatch):
    monkeypatch.setattr("services.token_vault.encrypt_token", lambda t: "ENC:" + t)
    p = _Pool()
    peer = {"peer_user_id": 9, "peer_name": "Боб", "peer_username": "bob"}
    run = asyncio.new_event_loop().run_until_complete
    assert run(ui.record_incoming(p, 1, 11, peer, 100, "да")) is True
    assert run(ui.record_incoming(p, 1, 11, peer, 100, "да")) is False
    assert run(ui.record_incoming(p, 1, 11, {"peer_user_id": None}, 101, "x")) is False


def test_listener_writes_private_messages_to_inbox(monkeypatch):
    from services import audience_listener as al

    seen = {}

    async def _rec(pool, owner, acc, peer, msg_id, text, date):
        seen.update(owner=owner, acc=acc, msg_id=msg_id, text=text, peer=peer)
        return True

    async def _scan(*a, **k):
        seen["scanned"] = True

    monkeypatch.setattr(ui, "record_incoming", _rec)
    monkeypatch.setattr("services.intent_sensor.scan_incoming", _scan)

    class _Msg:
        message = "интересно, сколько?"
        id = 555
        date = T0

    class _Sender:
        id = 42
        first_name = "Ира"
        last_name = None
        username = "ira"

    class _Ev:
        message = _Msg()
        is_private = True
        sender_id = 42

        async def get_sender(self):
            return _Sender()

    asyncio.new_event_loop().run_until_complete(
        al._on_incoming_message(object(), None, 77, _Ev(), 31))
    assert seen["acc"] == 31 and seen["owner"] == 77 and seen["msg_id"] == 555
    assert seen["peer"]["peer_user_id"] == 42
    assert seen.get("scanned") is True


# ── Карточка клиента ──────────────────────────────────────────────────────

def test_journey_answers_four_questions():
    j = tl.build_journey(
        sources=[{"label": "бот @shop", "at": _h(5)}, {"label": "аккаунт #3", "at": _h(1)}],
        received=[{"campaign": "Осень", "status": "sent", "at": _h(2)},
                  {"campaign": "Осень", "status": "failed", "at": _h(2)}],
        replies=[{"channel": "аккаунт #3", "text": "а цена?", "at": _h(3)}],
        orders=[{"total_cents": 150000, "currency": "RUB", "status": "done", "at": _h(4)},
                {"total_cents": 99900, "currency": "RUB", "status": "cancelled", "at": _h(4)}],
        funnel={"value": "purchased", "label": "Купил"})
    assert j["source"]["label"] == "аккаунт #3"       # самый ранний источник
    assert j["received"]["count"] == 1                 # недоставленное не «получил»
    assert j["received"]["campaigns"] == ["Осень"]
    assert j["replied"]["count"] == 1 and j["replied"]["last_text"] == "а цена?"
    assert j["bought"]["orders"] == 1                  # отменённый — не покупка
    assert j["bought"]["revenue"] == [{"currency": "RUB", "total_cents": 150000,
                                       "text": "1 500 RUB"}]


def test_timeline_shows_received_replies_and_orders():
    items = tl.build_timeline(
        received=[{"campaign": "Осень", "at": _h(1)}],
        replies=[{"channel": "Хранилище", "text": "да", "at": _h(2)}],
        orders=[{"total_cents": 500, "currency": "USD", "status": "confirmed", "at": _h(3)}])
    assert [i["kind"] for i in items] == ["order", "reply", "received"]
    assert items[0]["detail"] == "5 USD"


# ── Воронка денег ─────────────────────────────────────────────────────────

def test_attribution_is_last_touch_within_window():
    touches = [(1, 100, _h(0)), (2, 100, _h(10)), (1, 200, _h(0))]
    b = mf.attribute(
        touches,
        subscriptions=[(200, _h(1))],
        replies=[(100, _h(11)), (100, _h(5)), (300, _h(1))],
        orders=[(100, _h(12), 10000, "RUB"),
                (200, _h(0) + timedelta(days=31), 5000, "RUB")])
    assert b[1]["reached"] == {100, 200}
    assert b[2]["reached"] == {100}
    # Ответ в 5:00 — до кампании 2, значит кампании 1; в 11:00 — кампании 2.
    assert b[1]["replied"] == {100} and b[2]["replied"] == {100}
    assert b[1]["subscribed"] == {200}
    assert b[2]["bought"] == {100} and b[2]["revenue"] == {"RUB": 10000}
    # Покупка через 31 день после касания — уже не заслуга кампании.
    assert b[mf.NO_CAMPAIGN]["bought"] == {200}
    # Ответ человека, которому никто не писал, — органика.
    assert 300 in b[mf.NO_CAMPAIGN]["replied"]


def test_event_before_any_touch_is_not_credited():
    b = mf.attribute([(1, 5, _h(10))], replies=[(5, _h(1))])
    assert b[1]["replied"] == set()
    assert b[mf.NO_CAMPAIGN]["replied"] == {5}


def test_summary_counts_people_and_cost_per_lead():
    b = mf.attribute(
        [(1, u, _h(0)) for u in range(1, 101)],
        replies=[(u, _h(1)) for u in range(1, 11)] + [(1, _h(2))],
        orders=[(1, _h(3), 300000, "RUB"), (1, _h(4), 100000, "RUB")])
    s = mf.summarize(b, {1: "Осень"}, {1: {"amount": 5000, "currency": "RUB"}})
    row = s["campaigns"][0]
    assert row["name"] == "Осень"
    assert row["steps"] == {"reached": 100, "subscribed": 0, "replied": 10, "bought": 1}
    assert row["rates"]["replied"] == 0.1
    assert row["orders"] == 2
    assert row["cost_per_lead"] == 500.0
    assert row["cost_per_buyer"] == 5000.0
    # 4000 ₽ выручки на 5000 ₽ расхода — минус 20 %.
    assert row["romi"] == -0.2
    steps = {x["key"]: x["count"] for x in s["steps"]}
    assert steps == {"reached": 100, "subscribed": 0, "replied": 10, "bought": 1}
    assert s["cost_per_lead"] == 500.0


def test_romi_is_not_invented_across_currencies():
    b = mf.attribute([(1, 1, _h(0))], orders=[(1, _h(1), 10000, "USD")])
    s = mf.summarize(b, spend={1: {"amount": 100, "currency": "RUB"}})
    assert s["campaigns"][0]["romi"] is None


def test_small_samples_are_never_called_significant():
    b = mf.attribute(
        [(1, u, _h(0)) for u in range(10)] + [(2, u, _h(0)) for u in range(100, 110)],
        replies=[(u, _h(1)) for u in range(5)])
    s = mf.summarize(b)
    worse = [r for r in s["campaigns"] if r["campaign_id"] == 2][0]
    assert worse["vs_best"]["significant"] is False
    assert worse["vs_best"]["enough_data"] is False
    assert s["best_campaign_id"] == 1


def test_large_gap_on_large_samples_is_significant():
    b = mf.attribute(
        [(1, u, _h(0)) for u in range(500)] + [(2, u, _h(0)) for u in range(1000, 1500)],
        replies=[(u, _h(1)) for u in range(100)] + [(u, _h(1)) for u in range(1000, 1020)])
    s = mf.summarize(b)
    worse = [r for r in s["campaigns"] if r["campaign_id"] == 2][0]
    assert worse["vs_best"]["significant"] is True


def test_set_spend_rejects_foreign_campaign_and_bad_amount():
    class P:
        async def fetchval(self, q, *a):
            return None

        async def execute(self, q, *a):
            raise AssertionError("чужую кампанию писать нельзя")

    run = asyncio.new_event_loop().run_until_complete
    import pytest
    with pytest.raises(LookupError):
        run(mf.set_spend(P(), 1, 99, 100))
    with pytest.raises(ValueError):
        run(mf.set_spend(P(), 1, 99, "много"))
    with pytest.raises(ValueError):
        run(mf.set_spend(P(), 1, 99, -5))
