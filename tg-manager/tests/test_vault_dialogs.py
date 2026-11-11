"""Хранилище как список диалогов: чистые части без базы.

Жалоба владельца (09.11.2026): «1 клиентов ждут ответа» пришло утром и снова
вечером про одного и того же человека, «8 ходят по кругу» — каждые 6 часов,
хотя клиентам уже ответили, а часть уже воспользовалась услугой. Живые
SQL-случаи — в test_vault_dialogs_postgres.py.
"""
from __future__ import annotations

import datetime as dt

import pytest

from services import vault_analytics, vault_dialogs as vd
from services.organism import brain, runner


@pytest.mark.parametrize("text", [
    "Спасибо!", "спасибо большое 🙏", "Ок, договорились", "👍", "Понял, спасибо",
    "thanks", "Хорошо, до встречи",
])
def test_closing_replies_do_not_wait(text):
    assert vd.is_closing_message(text)


@pytest.mark.parametrize("text", [
    "Спасибо, а сколько стоит?", "Сколько ехать будет", "Новый Арбат 25",
    "Хочу записаться на завтра", "ок?",
])
def test_real_questions_wait(text):
    assert not vd.is_closing_message(text)


def test_sticker_closes_photo_does_not():
    assert vd.is_closing_message(None, "sticker")
    assert not vd.is_closing_message(None, "photo")
    assert not vd.is_closing_message("", "voice")


def _row(**kw):
    t = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
    base = {"last_in": t, "last_out": None, "live_direction": "in",
            "handled_at": None, "purchased": False, "archived": False,
            "muted": False, "_live_text": "Сколько стоит?", "live_media_type": None}
    base.update(kw)
    return base


def test_waiting_reason_covers_every_way_out():
    t = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
    later = t + dt.timedelta(minutes=5)
    assert vd.waiting_reason(_row()) is None
    assert vd.waiting_reason(_row(last_out=later)) == "answered"
    assert vd.waiting_reason(_row(live_direction="out")) == "answered"
    assert vd.waiting_reason(_row(handled_at=later)) == "handled"
    assert vd.waiting_reason(_row(handled_at=t - dt.timedelta(hours=1))) is None
    assert vd.waiting_reason(_row(purchased=True)) == "purchased"
    assert vd.waiting_reason(_row(archived=True)) == "archived"
    assert vd.waiting_reason(_row(muted=True)) == "muted"
    assert vd.waiting_reason(_row(_live_text="Спасибо!")) == "closing"
    assert vd.waiting_reason(_row(last_in=None)) == "no_incoming"


def test_response_stats_counts_turns_not_messages():
    t0 = dt.datetime(2026, 11, 9, 10, tzinfo=dt.timezone.utc)

    def m(d, minutes):
        return {"direction": d, "msg_date": t0 + dt.timedelta(minutes=minutes)}

    rows = [m("in", 0), m("in", 1), m("in", 2), m("out", 10),   # ответ 10 мин
            m("in", 70), m("out", 100),                          # ответ 30 мин
            m("in", 200)]                                        # висит
    st = vault_analytics.response_stats(rows)
    assert st["owner_replies"] == 2
    assert st["owner_reply_median_min"] == 20.0
    assert st["peer_reply_median_min"] == 80.0       # 60 и 100 мин → медиана 80
    assert st["owner_unanswered"] == 1


def test_same_people_are_not_nudged_twice():
    s = {"id": "vault_waiting", "subjects": ["1@a"]}
    assert not runner.already_told(s, {})
    assert runner.already_told(s, {"vault_waiting": ["1@a"]})
    # Один из двух ответил — нового ничего, молчим.
    assert runner.already_told(s, {"vault_waiting": ["1@a", "2@b"]})
    # Тот же клиент написал снова — это новый повод.
    assert not runner.already_told({"id": "vault_waiting", "subjects": ["1@c"]},
                                   {"vault_waiting": ["1@a"]})
    # Подсказки без людей живут по старому кулдауну.
    assert not runner.already_told({"id": "vault_off"}, {"vault_off": ["x"]})


def _waiting_snap(n):
    return {"vault": {"health": "ok", "waiting_reply": n,
                      "waiting": [{"chat_id": 100 + i, "name": f"Клиент{i}",
                                   "since": f"2026-11-09T1{i}:00:00+00:00"}
                                  for i in range(n)]}}


def test_waiting_nudge_grammar_names_and_target():
    one = [s for s in brain.build_suggestions(_waiting_snap(1), now=1)
           if s["id"] == "vault_waiting"][0]
    assert one["title"] == "1 клиент ждёт ответа"
    assert "Клиент0" in one["why"]
    assert one["action"] == {"kind": "vault", "op_id": 100}
    assert one["subjects"] == ["100@2026-11-09T10:00:00+00:00"]
    three = [s for s in brain.build_suggestions(_waiting_snap(3), now=1)
             if s["id"] == "vault_waiting"][0]
    assert three["title"] == "3 клиента ждут ответа"
    assert three["action"]["op_id"] == "waiting"
    five = [s for s in brain.build_suggestions(_waiting_snap(5), now=1)
            if s["id"] == "vault_waiting"][0]
    assert five["title"] == "5 клиентов ждут ответа"
    assert "и ещё 2" in five["why"]


def test_bouncing_nudge_grammar_and_subjects():
    snap = {"vlayer": {"bouncing": 8, "bouncing_ids": ["a", "b"]}}
    s = [x for x in brain.build_suggestions(snap, now=1)
         if x["id"] == "vlayer_bouncing"][0]
    assert s["title"] == "8 человек ходят по кругу"
    assert s["subjects"] == ["a", "b"]
    snap = {"vlayer": {"bouncing": 3}}
    s = [x for x in brain.build_suggestions(snap, now=1)
         if x["id"] == "vlayer_bouncing"][0]
    assert s["title"] == "3 человека ходят по кругу"


def test_bot_dialog_row_reads_like_a_chat_list():
    from bot.handlers.business_vault import _row_label
    now = dt.datetime.now(dt.timezone.utc)
    label = _row_label({"peer_name": "Анна", "last_preview": "Ты тут?",
                        "unread": 2, "awaiting_reply": True, "pinned": True,
                        "last_direction": "in",
                        "last_date": (now - dt.timedelta(minutes=5)).isoformat()})
    assert label.startswith("📌🔵2⏳ Анна: Ты тут?") and label.endswith("5 мин")
    own = _row_label({"peer_name": "Борис", "last_preview": "Жду",
                      "last_direction": "out", "last_date": None})
    assert own == "Борис: Вы: Жду"
