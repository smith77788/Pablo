"""Владелец не читает по-английски, и это касается не только подписей кнопок.

ЧТО ОТКЛОНЕНО И ПОЧЕМУ. Первым делом я померил прямое: латинские слова
строчными в видимом тексте разметки. Их 49, и настоящей английской фразы среди
них нет ни одной — расширения файлов (.csv, .xlsx, .db, .session), плейсхолдеры
({name}, {link}, {first_name}), куски адресов (t.me/channel, 5sim.net, utm_source,
socks5://), суффиксы username'а ботов (app, ai, agent, game), термины Telegram
(tdata, premium, username). Правило «латинское слово строчными — английское»
пришлось бы закрывать перечнем ровно этих 49, то есть перечнем самих находок, —
а это дырка, про которую в CLAUDE.md есть отдельный разбор.

ЧТО ОСТАЛОСЬ. Английскую ФРАЗУ выдают служебные слова: the, and, with, for,
you, your, are, this, that… Их список — не перечень исключений, а признак:
новый технический токен никогда не потребует его трогать, а любая английская
фраза хотя бы одно из них содержит. Сегодня в 2614 видимых кусках разметки их
ноль.

ГРАНИЦА. Проверяется ВИДИМЫЙ текст разметки: из `.js` надёжно выделить
пользовательские строки нельзя — проба дала 32 «находки», и все до одной
оказались именами полей кода (`scheduled_for`, `company_about`, `can_smalltalk`).
Подписи в коде стережёт соседний `test_mini_app_labels_are_russian`.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

#: Признак английской фразы, а НЕ перечень запрещённых слов: новый технический
#: токен (расширение, домен, плейсхолдер) этот список трогать не заставит.
_MARKERS = ("the", "and", "with", "for", "you", "your", "are", "this", "that",
            "please", "click", "from", "not", "have", "will", "can", "was",
            "were", "been", "there", "what", "when", "which", "would",
            "should", "could", "about", "into", "than", "then", "them",
            "they", "our", "its")
# Подчёркивание по краям исключает имена полей: `scheduled_for`, `company_about`.
_ENGLISH = re.compile(
    r"(?<![A-Za-z_])(" + "|".join(_MARKERS) + r")(?![A-Za-z_])", re.I)


def _visible() -> list[str]:
    src = miniapp_html()
    src = re.sub(r"<script[\s\S]*?</script>", " ", src)
    src = re.sub(r"<style[\s\S]*?</style>", " ", src)
    return [t.strip() for t in re.findall(r">([^<>]+)<", src) if t.strip()]


def test_the_detector_sees_the_text_at_all():
    """«Английского нет» на сотне кусков текста не значит ничего."""
    n = len(_visible())
    assert n >= 2000, (
        f"видимого текста нашлось всего {n} кусков — сломан разбор, а не "
        "разметка")


def test_no_english_sentence_is_shown_to_the_owner():
    bad = [(sorted({m.lower() for m in _ENGLISH.findall(t)}), t[:80])
           for t in _visible() if _ENGLISH.search(t)]
    assert not bad, (
        f"английские фразы в видимом тексте (проверено {len(_visible())} "
        f"кусков): {bad[:5]}")


def test_the_detector_still_catches_english():
    """Мера, которая не ловит, ничего не доказывает своим нулём."""
    assert _ENGLISH.search("Please click the button to continue")
    assert _ENGLISH.search("This account is not available")


def test_a_technical_token_is_not_english():
    """Ровно та причина, по которой правило «любое латинское слово» отклонено."""
    for ok in ("utm_source", "t.me/channel", ".xlsx / .csv / .db", "{first_name}",
               "socks5:// http://", "app · ai · agent · game", "📦 tdata",
               "5sim.net", "@username", "scheduled_for", "company_about"):
        assert not _ENGLISH.search(ok), f"технический токен принят за фразу: {ok}"
