"""Шаг 2 усиления «Модели поведения»: эмодзи-реакции доходят до слоя.

Реакция на сообщение бота — частая лёгкая вовлечённость, но Telegram НЕ шлёт
message_reaction без явного запроса в allowed_updates, и опрос дочерних ботов
его не запрашивал — слой этого сигнала не видел вовсе. Теперь опрос запрашивает
message_reaction (не задев «Хранилище»: бизнес-апдейты идёт главным ботом, не
этим опросом), а цикл шлёт сигнал «reacted» на ДОБАВЛЕНИЕ/смену реакции.

«reacted» — слабый сигнал: контакт он не заводит (иначе CRM утонет в реагирующих
подписчиках, см. test_bot_subscriber_reaches_the_layer), но двигает тех, кто в
контактах уже есть. Это проверяется там же; здесь — разбор апдейта и проводка.
"""
from __future__ import annotations

import inspect

from services import auto_responder as AR
from services import virtual_layer as vl

U = {"id": 555, "username": "vasya", "first_name": "Вася"}


# ── разбор апдейта message_reaction ───────────────────────────────────────────

def test_added_reaction_yields_user_and_identity():
    uid, who = AR.reaction_signal(
        {"user": U, "old_reaction": [], "new_reaction": [{"emoji": "👍"}]})
    assert uid == 555 and who == U


def test_changed_reaction_still_signals():
    uid, _ = AR.reaction_signal(
        {"user": U, "old_reaction": [{"emoji": "👍"}],
         "new_reaction": [{"emoji": "🔥"}]})
    assert uid == 555


def test_removed_reaction_is_not_engagement():
    # сняли реакцию — это не интерес, сигнала нет
    uid, who = AR.reaction_signal(
        {"user": U, "old_reaction": [{"emoji": "👍"}], "new_reaction": []})
    assert uid is None and who is None


def test_noop_same_reaction_is_ignored():
    r = [{"emoji": "👍"}]
    assert AR.reaction_signal({"user": U, "old_reaction": r, "new_reaction": r}) == (None, None)


def test_anonymous_reaction_has_no_user():
    # реакция от чата (анонимный админ/канал) — некого двигать по воронке
    uid, who = AR.reaction_signal(
        {"actor_chat": {"id": -100}, "new_reaction": [{"emoji": "👍"}]})
    assert uid is None and who is None


def test_garbage_is_safe():
    assert AR.reaction_signal(None) == (None, None)
    assert AR.reaction_signal({}) == (None, None)


# ── проводка опроса и цикла ───────────────────────────────────────────────────

def test_poll_requests_reactions_without_dropping_what_is_handled():
    au = AR._MANAGED_BOT_ALLOWED_UPDATES
    # запрашиваем реакции — иначе Telegram их не доставит
    assert "message_reaction" in au
    # и НЕ теряем то, что цикл реально обрабатывает
    for needed in ("message", "callback_query", "my_chat_member"):
        assert needed in au, f"опрос перестанет получать {needed}"
    # бизнес-апдейты сюда НЕ входят намеренно (их доставляет главный бот) —
    # сужение набора не должно задеть «Хранилище»
    assert not any(a.startswith("business") for a in au)


def test_getupdates_passes_allowed_updates():
    src = inspect.getsource(AR)
    # оба getUpdates (инициализация оффсета и основной опрос) с явным списком
    assert src.count("allowed_updates=_MANAGED_BOT_ALLOWED_UPDATES") >= 2


def test_loop_signals_reacted_on_message_reaction():
    src = inspect.getsource(AR)
    i = src.index('upd.get("message_reaction")')
    seg = src[i:i + 400]
    assert "reaction_signal(mr)" in seg
    assert '"reacted", 0.5' in seg       # ступень «присматривается», слабый сигнал


def test_reacted_is_a_known_weak_signal():
    # сигнал существует в словаре переходов и остаётся слабым (контакт не заводит)
    assert "reacted" in vl.SIGNAL_TARGET
    assert "reacted" not in vl._SIGNALS_WORTH_A_CONTACT
    assert vl.SIGNAL_LABEL.get("reacted")   # и читается по-русски (шаг 1)
