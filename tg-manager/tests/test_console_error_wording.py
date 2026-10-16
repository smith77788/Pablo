"""Консоль аккаунта: пауза Telegram называется числом и по-русски.

Владелец не читает по-английски — это правило проекта, и оно нарушалось здесь
тихо. `classify_error` узнавала флуд по слову «flood», а telethon на ожидание
слоу-мода отдаёт текст без него: «A wait of 45 seconds is required before
sending another message in this chat». Такое сообщение проваливалось в
последнюю ветку и уезжало владельцу как есть, по-английски, да ещё под видом
неизвестной ошибки.

Второе: даже узнанный флуд сообщал «слишком часто» без срока. По сроку видно,
отдохнуть минуту или отложить работу на час, — а это разные решения.
"""
from __future__ import annotations

import pytest

from services.account_console import classify_error, human_wait


@pytest.mark.parametrize("seconds,expected", [
    (0, "0 с"),
    (45, "45 с"),
    (59, "59 с"),
    (60, "1 мин"),
    (300, "5 мин"),
    (3599, "59 мин"),
    (3600, "1 ч"),
    (5400, "1 ч 30 мин"),
    (7200, "2 ч"),
])
def test_human_wait_speaks_russian(seconds, expected):
    assert human_wait(seconds) == expected


def test_human_wait_survives_garbage():
    assert human_wait(-5) == "0 с"
    assert human_wait(None) == "0 с"


def test_slow_mode_is_not_reported_as_an_unknown_error():
    """Самое частое ожидание при отправке в чат — и оно уезжало по-английски."""
    raw = ("A wait of 45 seconds is required before sending another message "
           "in this chat (caused by SendMessageRequest)")
    code, human = classify_error(raw)

    assert code == "slow_mode", f"слоу-мод не распознан, код {code!r}"
    assert "медленный режим" in human
    assert "45 с" in human
    assert "wait" not in human.lower(), "английский текст утёк владельцу"


def test_flood_reports_how_long_to_wait():
    code, human = classify_error("A wait of 3600 seconds is required")

    assert code == "flood"
    assert "1 ч" in human, f"срок паузы потерян: {human!r}"


def test_handoff_from_the_guard_is_also_understood():
    """Предохранитель отдаёт наверх свой FloodHandoff — его текст тоже наш."""
    code, human = classify_error("FloodWait handoff: 45s")

    assert code == "flood"
    assert "45 с" in human


@pytest.mark.parametrize("raw,code", [
    ("The key is invalid", "session_expired"),
    ("AUTH_KEY_UNREGISTERED", "session_expired"),
    ("Too many requests (PeerFloodError)", "flood"),
    ("privacy settings restricted", "privacy"),
    ("CHAT_WRITE_FORBIDDEN", "no_access"),
    ("socks5 proxy unreachable", "network"),
    ("Could not find the input entity", "not_found"),
])
def test_existing_classifications_are_untouched(raw, code):
    """Новая ветка не должна перехватывать чужие случаи."""
    assert classify_error(raw)[0] == code


def test_every_message_is_russian():
    """Ни один ответ классификатора не уходит владельцу по-английски."""
    samples = [
        "A wait of 45 seconds is required before sending another message in this chat",
        "A wait of 3600 seconds is required",
        "FloodWait handoff: 10s",
        "The key is invalid",
        "privacy settings restricted",
        "CHAT_WRITE_FORBIDDEN",
        "socks5 proxy unreachable",
    ]
    for raw in samples:
        human = classify_error(raw)[1]
        assert any("а" <= ch.lower() <= "я" for ch in human), (
            f"ответ без единой русской буквы: {human!r}"
        )
