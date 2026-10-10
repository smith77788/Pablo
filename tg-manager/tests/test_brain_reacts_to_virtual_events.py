"""Мозг реагирует на то, что слой ПОНЯЛ, а не только на накопленные счётчики.

ЧТО БЫЛО. Виртуальный слой рождал события — «дошёл до решения», «остыл», «бот
разогрелся» — и складывал их в журнал. Дальше не происходило ничего: подсказки
мозга строились на счётчиках состояний («сколько всего готовых»). Разница не
косметическая:

* счётчик не меняется от того, что человек дошёл до решения минуту назад, —
  мозг повторял одно и то же про один и тот же остывающий список;
* на остывание не реагировал никто, хотя это вторая половина слоя: реактивация
  по только что остывшему дешевле нового трафика, а через неделю поздно;
* температура ботов считалась, показывалась цифрой — и не превращалась ни в
  какое действие.

ЧТО ТЕПЕРЬ. Снимок мира несёт события за сутки, и мозг строит на них три
подсказки. Свежие решения вытесняют подсказку по накопленному списку: две
подсказки про одно и то же — это шум, а нудж у владельца один на два часа.
"""
from __future__ import annotations

from services.organism.brain import build_suggestions


def _snap(**vlayer):
    return {"fleet": {}, "ops": {}, "graph": {}, "vault": {}, "vlayer": vlayer}


def _ids(snap):
    return {s["id"] for s in build_suggestions(snap, now=1_000_000)}


def _by_id(snap, sid):
    return next(s for s in build_suggestions(snap, now=1_000_000)
                if s["id"] == sid)


def test_fresh_decisions_raise_an_urgent_suggestion():
    ids = _ids(_snap(became_ready_24h=3, ready=0))
    assert "vlayer_intent_now" in ids
    assert _by_id(_snap(became_ready_24h=3), "vlayer_intent_now")["severity"] == "urgent"


def test_one_or_two_fresh_decisions_do_not_nudge():
    """Порог есть, чтобы не дёргать владельца по одному лиду."""
    assert "vlayer_intent_now" not in _ids(_snap(became_ready_24h=2))


def test_fresh_decisions_replace_the_accumulated_list():
    ids = _ids(_snap(became_ready_24h=4, ready=40, audience="hot"))
    assert "vlayer_intent_now" in ids
    assert "vlayer_hot" not in ids, "две подсказки про одно и то же — это шум"
    assert "vlayer_audience" not in ids


def test_accumulated_list_still_works_without_fresh_events():
    ids = _ids(_snap(became_ready_24h=0, ready=9))
    assert "vlayer_hot" in ids, "старое поведение потеряно"


def test_cooling_finally_leads_somewhere():
    ids = _ids(_snap(cooled_24h=5))
    assert "vlayer_cooling" in ids, (
        "событие «остыл» снова никуда не ведёт — слой понял, продукт промолчал")
    assert "vlayer_cooling" not in _ids(_snap(cooled_24h=4))


def test_cooling_and_fresh_decisions_can_coexist():
    """Это разные люди и разные действия: оффер и реактивация."""
    ids = _ids(_snap(became_ready_24h=3, cooled_24h=6))
    assert {"vlayer_intent_now", "vlayer_cooling"} <= ids


def test_hot_bot_becomes_an_action():
    s = _by_id(_snap(hot_bot="shop_bot"), "vlayer_bot_hot")
    assert "@shop_bot" in s["title"], s["title"]
    assert s["action"] == {"kind": "vlayer"}


def test_hot_bot_name_is_not_double_prefixed():
    assert "@@" not in _by_id(_snap(hot_bot="@shop_bot"), "vlayer_bot_hot")["title"]


def test_no_vlayer_suggestions_on_an_empty_layer():
    assert not {i for i in _ids(_snap()) if i.startswith("vlayer_")}


def test_suggestions_speak_russian():
    snap = _snap(became_ready_24h=5, cooled_24h=7, hot_bot="shop_bot")
    for s in build_suggestions(snap, now=1_000_000):
        if not s["id"].startswith("vlayer_"):
            continue
        for field in ("title", "why"):
            text = s[field].replace("@shop_bot", "")
            assert not any("a" <= c.lower() <= "z" for c in text), (s["id"], field)


def test_snapshot_carries_the_event_counters():
    """Подсказка без данных в снимке не сработает никогда."""
    import inspect
    from services.organism import world
    src = inspect.getsource(world._vlayer)
    for key in ("became_ready_24h", "cooled_24h", "hot_bot"):
        assert key in src, f"{key} не собирается в снимке мира"
    assert "purchase_intent_detected" in src and "user_lost_interest" in src
