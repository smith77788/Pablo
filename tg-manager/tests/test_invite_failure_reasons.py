"""Отказы инвайта: настоящие корзины вместо «прочее» и подсказка оператору.

ЧТО БЫЛО СЛОМАНО. Движок ловил горстку известных ошибок Telethon, а ВСЁ
остальное складывал в `f"{ref}: {str(e)[:80]}"`. Исполнитель потом угадывал
причину, ища подстроки «privacy» / «not mutual» / «flood» в английском тексте.
На практике крупнейшие корзины отказов — мёртвый username, удалённый аккаунт,
«цель уже в предельном числе чатов» — не совпадали ни с одной подстрокой и
уходили в «❓ прочее». Оператор видел «Ошибок: 812 (❓ прочее: 812)» и не мог
понять, что чинить: аудиторию, права или аккаунты.
"""
from __future__ import annotations

import pytest

from services import mass_inviter_engine as mie


class _Err(Exception):
    """Подделка ошибки Telethon: классификация идёт по ИМЕНИ класса."""


def _named(name, text=""):
    return type(name, (_Err,), {})(text)


@pytest.mark.parametrize("name,expected", [
    ("UserPrivacyRestrictedError", mie.FAIL_PRIVACY),
    ("UserNotMutualContactError", mie.FAIL_NOT_MUTUAL),
    ("UserChannelsTooMuchError", mie.FAIL_TOO_MANY_CHATS),
    ("UserBannedInChannelError", mie.FAIL_BANNED),
    ("UserKickedError", mie.FAIL_BANNED),
    ("UserBlockedError", mie.FAIL_BLOCKED),
    ("InputUserDeactivatedError", mie.FAIL_DEAD),
    ("UsernameNotOccupiedError", mie.FAIL_DEAD),
    ("PeerIdInvalidError", mie.FAIL_DEAD),
    ("FloodWaitError", mie.FAIL_FLOOD),
])
def test_classifies_by_telethon_error_name(name, expected):
    assert mie.classify_invite_error(_named(name)) == expected


@pytest.mark.parametrize("text,expected", [
    ("No user has \"vasya\" as username", mie.FAIL_DEAD),
    ("Cannot find any entity corresponding to \"@ghost\"", mie.FAIL_DEAD),
    ("The username is not occupied by anyone", mie.FAIL_DEAD),
    ("You have joined too much channels/supergroups", mie.FAIL_TOO_MANY_CHATS),
    ("privacy settings do not allow this", mie.FAIL_PRIVACY),
])
def test_classifies_plain_valueerror_by_text(text, expected):
    """Часть случаев Telethon отдаёт обычным ValueError без своего типа."""
    assert mie.classify_invite_error(ValueError(text)) == expected


def test_unknown_error_stays_other():
    assert mie.classify_invite_error(RuntimeError("что-то новое")) == mie.FAIL_OTHER


def test_every_bucket_has_a_human_label():
    for bucket in (mie.FAIL_PRIVACY, mie.FAIL_NOT_MUTUAL, mie.FAIL_DEAD,
                   mie.FAIL_TOO_MANY_CHATS, mie.FAIL_BANNED, mie.FAIL_BLOCKED,
                   mie.FAIL_FLOOD, mie.FAIL_PERM, mie.FAIL_OTHER):
        assert mie.FAIL_LABELS.get(bucket), f"корзина {bucket} без человеческой метки"


def test_actionable_buckets_have_advice():
    """Цифра без действия оператору бесполезна: «нет в Telegram: 812» должно
    означать «почистите базу», а «приватность: 812» — «смените метод»."""
    for bucket in (mie.FAIL_PRIVACY, mie.FAIL_NOT_MUTUAL, mie.FAIL_DEAD,
                   mie.FAIL_TOO_MANY_CHATS, mie.FAIL_BANNED, mie.FAIL_BLOCKED):
        assert mie.FAIL_ADVICE.get(bucket), f"корзина {bucket} без подсказки «что делать»"
