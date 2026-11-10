"""Разведка рекламы понимает вставленную ссылку и отвечает по-русски.

ЧТО БЫЛО (сводка частых сбоев, операция №811). С ввода срезалась только «@»,
поэтому вставленная ссылка уходила в Telegram как «@https://t.me/name».
Операция срывалась через минуту с текстом «Cannot find any entity
corresponding to …» — по-английски, которого владелец не читает, и без
подсказки, что делать.
"""
from __future__ import annotations

import asyncio

import pytest

from services import ad_intelligence as ai


@pytest.mark.parametrize("given", [
    "durov", "@durov", " @durov ", "t.me/durov", "https://t.me/durov",
    "http://t.me/durov/123", "https://t.me/s/durov", "https://telegram.me/durov?x=1",
    "T.ME/durov", "@https://t.me/durov",
])
def test_every_way_a_person_names_a_public_channel_gives_its_name(given):
    assert ai.channel_username_from(given) == "durov"


@pytest.mark.parametrize("given", [
    "", "   ", "https://t.me/+AbCdEf123456", "t.me/joinchat/AbCdEf", "@ab",
    "привет", "два слова", "1abcd",
])
def test_what_is_not_a_public_channel_is_refused_not_sent_to_telegram(given):
    assert ai.channel_username_from(given) == ""


def test_the_reason_is_in_russian_and_says_what_to_do():
    text = ai.channel_open_error(
        "durov", ValueError('Cannot find any entity corresponding to "@durov"'))
    assert "не найден" in text and "@durov" in text and "t.me/" in text
    assert "Cannot" not in text and "entity" not in text
    other = ai.channel_open_error("durov", RuntimeError("Some internal english text"))
    assert "english" not in other and "Не удалось" in other


def test_a_private_link_is_answered_before_any_account_is_touched():
    class _NoPool:
        def acquire(self):
            raise AssertionError("до базы и аккаунтов дело дойти не должно")

    res = asyncio.run(ai.scan_channel_ads(_NoPool(), "https://t.me/+AbCdEf123456", 1, 2))
    assert res["status"] == "error" and "публичный канал" in res["error"]
