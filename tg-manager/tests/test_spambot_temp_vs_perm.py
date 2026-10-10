"""Регресс: проверка СВОИХ аккаунтов различает временный и вечный спамблок.

@SpamBot на временный блок всегда называет срок/дату снятия, на вечный/бессрочный —
нет. `classify_spambot_restriction` раскладывает это в 'temp'/'perm', чтобы панель
аккаунтов могла раскидать по папкам «Временный/Вечный спамблок» (раньше оба
схлопывались в один status='spamblock').
"""
from __future__ import annotations

from services.account_manager import (
    classify_spambot_reply,
    classify_spambot_restriction,
)

# Реалистичные ответы @SpamBot.
TEMP_EN = (
    "I'm afraid you have been limited for violating Telegram's Terms of Service. "
    "The account will be automatically released on 25 Dec 2026."
)
TEMP_EN_UNTIL = (
    "Some Telegram features may be unavailable to you until 2026-12-25."
)
TEMP_RU = (
    "Ваш аккаунт ограничен за нарушение правил Telegram. "
    "Ограничение будет снято 25 декабря 2026."
)
PERM_EN = (
    "Unfortunately, your account is limited and it's not going to be lifted "
    "automatically."
)
PERM_RU = (
    "Ваш аккаунт ограничен. Ограничение не будет снято автоматически."
)
OK_EN = "Good news, no limits are currently applied to your account."
OK_RU = "Хорошие новости — на вашем аккаунте нет ограничений."


def test_temp_blocks_detected_as_temp():
    for reply in (TEMP_EN, TEMP_EN_UNTIL, TEMP_RU):
        assert classify_spambot_reply(reply) == "spamblock", reply
        assert classify_spambot_restriction(reply) == "temp", reply


def test_perm_blocks_detected_as_perm():
    for reply in (PERM_EN, PERM_RU):
        assert classify_spambot_reply(reply) == "spamblock", reply
        assert classify_spambot_restriction(reply) == "perm", reply


def test_perm_priority_over_automatic_wording():
    # «not going to be lifted automatically» содержит слово об автоснятии,
    # но по смыслу это ВЕЧНЫЙ блок — вечные признаки имеют приоритет.
    assert classify_spambot_restriction(PERM_EN) == "perm"


def test_unknown_block_without_deadline_defaults_perm():
    # Спамблок без названного срока — трактуем как вечный (требует внимания),
    # а не как временный, чтобы не гонять его в авто-ретраи впустую.
    reply = "Your account is limited."
    assert classify_spambot_reply(reply) == "spamblock"
    assert classify_spambot_restriction(reply) == "perm"


def test_ok_replies_not_spamblock():
    for reply in (OK_EN, OK_RU):
        assert classify_spambot_reply(reply) == "active", reply
