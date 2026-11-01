"""Максимум входных сигналов для «Модели поведения».

Слой кормят новые события, которых он раньше не видел:
- оплата через Telegram Payments (message.successful_payment) → «оплатил»;
- голос в опросе бота (poll_answer) → «присматривается»;
- вступление/выход участника чата-канала бота (chat_member) → «открыл»/«отписался»;
- встроенное намерение из ТЕКСТА без ручных правил (цена/оплата/вопрос) — поверх
  общего «ответил», и в Хранилище, и на аккаунт-стороне (один scan_incoming).

Чистые разборщики проверяем без Telegram; проводку — по исходнику; валидность
имён сигналов — по словарям слоя.
"""
from __future__ import annotations

import inspect

from services import auto_responder as A
from services import intent_sensor as I
from services import virtual_layer as vl


# ── встроенное намерение из текста ────────────────────────────────────────────

def test_builtin_intent_payment_beats_price_beats_question():
    assert I.builtin_intent_signal("Как оплатить заказ?") == "asked_how_to_pay"
    assert I.builtin_intent_signal("скиньте счёт, готов оплатить") == "asked_how_to_pay"
    assert I.builtin_intent_signal("а сколько стоит доставка") == "asked_price"
    assert I.builtin_intent_signal("какая цена?") == "asked_price"
    assert I.builtin_intent_signal("а когда будет?") == "asked_question"
    assert I.builtin_intent_signal("как это работает?") == "asked_question"
    assert I.builtin_intent_signal("что входит в комплект") == "asked_question"


def test_builtin_intent_none_on_neutral_or_empty():
    assert I.builtin_intent_signal("спасибо, всё понятно") is None
    assert I.builtin_intent_signal("") is None
    assert I.builtin_intent_signal(None) is None


# ── голос в опросе ────────────────────────────────────────────────────────────

def test_poll_vote_signals_on_vote_only():
    assert A.poll_vote_signal({"user": {"id": 5}, "option_ids": [1]}) == (5, {"id": 5})
    assert A.poll_vote_signal({"user": {"id": 5}, "option_ids": []}) == (None, None)
    assert A.poll_vote_signal({"voter_chat": {"id": -1}, "option_ids": [1]}) == (None, None)
    assert A.poll_vote_signal(None) == (None, None)


# ── участник чата/канала ──────────────────────────────────────────────────────

U = {"id": 9, "is_bot": False}


def test_chat_member_join_is_opened():
    uid, sig, who = A.chat_member_signal(
        {"old_chat_member": {"status": "left"},
         "new_chat_member": {"status": "member", "user": U}})
    assert (uid, sig, who) == (9, "opened", U)


def test_chat_member_leave_is_unsubscribed():
    uid, sig, _ = A.chat_member_signal(
        {"old_chat_member": {"status": "member"},
         "new_chat_member": {"status": "kicked", "user": U}})
    assert (uid, sig) == (9, "unsubscribed")


def test_chat_member_promote_is_not_a_funnel_event():
    # member → administrator: оба «внутри», воронку это не двигает
    assert A.chat_member_signal(
        {"old_chat_member": {"status": "member"},
         "new_chat_member": {"status": "administrator", "user": U}}) == (None, None, None)


def test_chat_member_ignores_bots_and_anon():
    assert A.chat_member_signal(
        {"old_chat_member": {"status": "left"},
         "new_chat_member": {"status": "member",
                             "user": {"id": 3, "is_bot": True}}}) == (None, None, None)
    assert A.chat_member_signal(
        {"old_chat_member": {"status": "left"},
         "new_chat_member": {"status": "member"}}) == (None, None, None)


# ── проводка ──────────────────────────────────────────────────────────────────

def test_allowed_updates_cover_new_types_without_dropping_old():
    au = A._MANAGED_BOT_ALLOWED_UPDATES
    for t in ("message", "callback_query", "my_chat_member", "message_reaction",
              "poll_answer", "chat_member"):
        assert t in au, f"опрос перестанет получать {t}"
    assert not any(x.startswith("business") for x in au)  # Хранилище не задето
    # Платёжный запрос теперь берём: счёт сценария и заказа ИИ-продавца
    # подтверждаются в опросе (auto_responder._answer_pre_checkout), а боты
    # со счётом в ожидании опрашиваются раз в секунду — 10 секунд хватает.
    assert "pre_checkout_query" in au


def test_loop_wires_payment_poll_and_member():
    src = inspect.getsource(A)
    # платёж → оплатил (поле message, до проверки текста)
    assert 'msg.get("successful_payment")' in src and '"paid", 0.98' in src
    # опрос → reacted
    i = src.index('upd.get("poll_answer")')
    assert '"reacted", 0.5' in src[i:i + 300]
    # участник → opened/unsubscribed
    j = src.index('upd.get("chat_member")')
    assert "chat_member_signal(cm)" in src[j:j + 300]


def test_intent_sensor_emits_builtin_intent():
    src = inspect.getsource(I.scan_incoming)
    assert "builtin_intent_signal(text)" in src
    assert "_intent" in src


def test_bot_dm_also_applies_builtin_intent():
    # «сколько стоит» в ЛС бота тоже должно двигать выше общего «ответил»
    src = inspect.getsource(A)
    assert "from services.intent_sensor import builtin_intent_signal" in src
    assert "builtin_intent_signal(text)" in src


# ── валидность имён сигналов ───────────────────────────────────────────────────

def test_all_new_signals_are_known_to_the_layer():
    for s in ("paid", "reacted", "opened", "asked_how_to_pay", "asked_price",
              "asked_question"):
        assert s in vl.SIGNAL_TARGET, f"{s} не двигает лестницу"
    assert "unsubscribed" in vl.NEGATIVE_SIGNALS
    # и все читаются по-русски (шаг 1 — reason_label/SIGNAL_LABEL)
    for s in ("paid", "reacted", "opened", "asked_how_to_pay", "asked_price",
              "asked_question", "unsubscribed"):
        assert vl.SIGNAL_LABEL.get(s), f"нет русской подписи сигнала {s}"
