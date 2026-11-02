"""Прогноз покупок (services/ai_predict) и подсказки по людям в копайлоте
(services/next_actions): чистые функции — без базы."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import ai_predict as ap
from services import next_actions as na

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def test_постоянный_в_ритме_лучший_клиент_с_высокой_вероятностью():
    s = ap.score({"dates": [_ago(40), _ago(26), _ago(12)]}, NOW)
    assert s["segment"] == "champions" and s["frequency"] == 3
    assert s["rhythm_days"] == 14.0 and s["probability"] >= 0.6


def test_пропустивший_ритм_под_угрозой_и_вероятность_гаснет():
    loyal = ap.score({"dates": [_ago(50), _ago(40)]}, NOW)          # ритм 10, прошло 40
    assert loyal["segment"] == "at_risk"
    fresh = ap.score({"dates": [_ago(20), _ago(10)]}, NOW)
    assert fresh["segment"] == "loyal"
    assert loyal["probability"] < fresh["probability"]


def test_разовые_новые_и_спящие():
    assert ap.score({"dates": [_ago(5)]}, NOW)["segment"] == "new"
    old = ap.score({"dates": [_ago(90)]}, NOW)
    assert old["segment"] == "sleeping" and old["probability"] < 0.1
    # Свежая переписка ПОСЛЕ покупки поднимает шанс второй.
    talk = ap.score({"dates": [_ago(90)], "last_msg_at": _ago(1)}, NOW)
    assert talk["probability"] > old["probability"]


def test_не_купившие_по_типу_реплики_и_свежести():
    hot = ap.score({"last_msg_at": _ago(0.5), "last_intent": "ready_to_buy"}, NOW)
    assert hot["segment"] == "hot_leads"
    cold = ap.score({"last_msg_at": _ago(2), "last_intent": "other"}, NOW)
    assert cold["segment"] == "leads" and cold["probability"] < hot["probability"]
    gone = ap.score({"last_msg_at": _ago(45), "last_intent": "price"}, NOW)
    assert gone["segment"] is None, "разговор старше месяца — не лид"


def test_покупки_из_будущего_не_считаются():
    s = ap.score({"dates": [_ago(-3)]}, NOW)
    assert s["frequency"] == 0


def test_score_all_сортирует_и_сводка_по_всем_сегментам():
    rows = ap.score_all([
        {"peer_user_id": 1, "dates": [_ago(90)]},
        {"peer_user_id": 2, "dates": [_ago(30), _ago(16), _ago(2)]},
        {"peer_user_id": 3, "last_msg_at": _ago(100)},
    ], NOW)
    assert [r["peer_user_id"] for r in rows] == [2, 1]
    summary = {s["id"]: s for s in ap.summarize(rows)}
    assert len(summary) == len(ap.SEGMENTS)
    assert summary["champions"]["count"] == 1 and summary["sleeping"]["count"] == 1
    assert summary["at_risk"]["tag"] == "прогноз_под_угрозой"


def test_проверка_точности_честно_молчит_на_малых_данных():
    few = [{"dates": [_ago(60)]} for _ in range(3)]
    bt = ap.backtest(few, NOW)
    assert bt == {"checked": 3, "enough": False}


def test_проверка_точности_сверяет_прогноз_с_покупками():
    # 6 постоянных в ритме (купили и в последний месяц) и 6 разовых давних (не купили).
    regular = [{"dates": [_ago(70), _ago(55), _ago(40), _ago(10)]} for _ in range(6)]
    one_off = [{"dates": [_ago(200)]} for _ in range(6)]
    bt = ap.backtest(regular + one_off, NOW)
    assert bt["enough"] and bt["checked"] == 12
    assert bt["actual"] == 0.5
    assert bt["top_third_actual"] == 1.0 and bt["rest_actual"] < bt["top_third_actual"]


def test_подсказки_по_людям_со_склонениями():
    s = {x["id"]: x for x in na.contact_suggestions(
        {"hot_waiting": 3, "complaints_waiting": 1, "at_risk": 5, "hot_leads": 21})}
    assert s["contacts_hot_waiting"]["title"] == "3 горячих ждут ответа больше часа"
    assert s["contacts_hot_waiting"]["fn"] == "openInboxWaiting"
    assert s["contacts_hot_waiting"]["snooze_hours"] == 3
    assert s["contacts_complaints_waiting"]["title"] == "1 жалоба без ответа"
    assert s["contacts_at_risk"]["title"].startswith("5 постоянных клиентов пропустили")
    assert s["contacts_hot_leads"]["title"].startswith("21 человек спрашивал ")
    assert na.contact_suggestions({}) == []


def test_ждущие_дольше_часа_горячие_и_жалобы():
    threads = [
        {"waiting": True, "last_at": (NOW - timedelta(hours=2)).isoformat(),
         "last_text": "Сколько стоит доставка?"},
        {"waiting": True, "last_at": NOW - timedelta(hours=3), "last_text": "Беру две"},
        {"waiting": True, "last_at": NOW - timedelta(minutes=20), "last_text": "Беру"},
        {"waiting": False, "last_at": NOW - timedelta(hours=5), "last_text": "Цена?"},
        {"waiting": True, "last_at": NOW - timedelta(hours=8), "last_text": "Верните деньги"},
        {"waiting": True, "last_at": NOW - timedelta(hours=8), "last_text": "Привет"},
    ]
    assert na.count_waiting(threads, NOW) == {"hot_waiting": 2, "complaints_waiting": 1}


def test_копайлот_учится_на_реакциях():
    items = [{"id": "a", "priority": 50}, {"id": "b", "priority": 60},
             {"id": "c", "priority": 90}, {"id": "hot", "priority": 98, "snooze_hours": 3}]
    stats = {
        "a": {"used": 4, "snoozed": 0},                                # 50 → 66
        "b": {"used": 0, "snoozed": 2, "last_snoozed": NOW - timedelta(days=2)},  # 60 → 48
        "c": {"used": 0, "snoozed": 1, "last_snoozed": NOW - timedelta(hours=5)},  # спрятана
        "hot": {"snoozed": 1, "last_snoozed": NOW - timedelta(hours=4)},  # 3 ч прошли
    }
    out = na.rank_with_feedback(items, stats, NOW)
    assert [i["id"] for i in out] == ["hot", "a", "b"]
    assert out[1]["priority"] == 66 and "пользуетесь" in out[1]["learned"]
    assert out[2]["priority"] == 48 and "откладываете" in out[2]["learned"]
    assert items[0]["priority"] == 50, "исходные подсказки не меняются"
    assert na.rank_with_feedback(items, {}, NOW)[0]["id"] == "hot"
