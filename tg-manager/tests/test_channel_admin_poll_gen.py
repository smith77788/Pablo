"""Нативные опросы виртуального администратора — слой генерации (вопрос + варианты).

Опрос — сильный легальный двигатель вовлечения в Telegram. Здесь проверяем ТОЛЬКО
генерацию и валидацию под лимиты Telegram (чистые функции). Публикация опроса в
живой канал — отдельный следующий срез.
"""
from __future__ import annotations

from services import channel_admin as ca


def test_build_poll_prompt_asks_for_json_and_uses_profile():
    system, user = ca.build_poll_prompt(
        {"topic": "финансы", "audience": "предприниматели", "lang": "ru"},
        topic_hint="налоги 2026")
    assert "JSON" in system and "options" in system
    assert "финансы" in user and "предприниматели" in user
    assert "налоги 2026" in user


def test_parse_poll_clean_and_fenced_and_alt_keys():
    assert ca.parse_poll('{"question":"Как вы платите?","options":["Картой","Налом"]}') == {
        "question": "Как вы платите?", "options": ["Картой", "Налом"]}
    fenced = "вот опрос:\n```json\n{\"question\":\"Q\",\"options\":[\"a\",\"b\"]}\n```"
    assert ca.parse_poll(fenced) == {"question": "Q", "options": ["a", "b"]}
    # альтернативные ключи
    assert ca.parse_poll('{"q":"Q","answers":["a","b"]}') == {
        "question": "Q", "options": ["a", "b"]}


def test_parse_poll_rejects_junk():
    assert ca.parse_poll("нет тут json") is None
    assert ca.parse_poll('{"question":"Q","options":"не список"}') is None


def test_validate_poll_trims_dedups_and_limits():
    clean, errors = ca.validate_poll("  Как   дела?  ",
                                     ["Отлично", "отлично", "  ", "Плохо"])
    assert errors == []
    assert clean["question"] == "Как дела?"
    assert clean["options"] == ["Отлично", "Плохо"]  # схлопнут дубль без регистра и пустой


def test_validate_poll_truncates_to_telegram_limits():
    clean, errors = ca.validate_poll("В" * 400, ["A" * 200, "B" * 10])
    assert errors == []
    assert len(clean["question"]) == ca._POLL_Q_MAX
    assert len(clean["options"][0]) == ca._POLL_OPT_MAX


def test_validate_poll_caps_options_count():
    clean, _ = ca.validate_poll("Q", [f"вариант {i}" for i in range(20)])
    assert len(clean["options"]) == ca._POLL_MAX_OPTS


def test_validate_poll_needs_two_distinct():
    none, errors = ca.validate_poll("Q", ["один", "один"])
    assert none is None and errors
    none2, errors2 = ca.validate_poll("", ["a", "b"])
    assert none2 is None and errors2


def test_clean_poll_end_to_end():
    raw = '{"question":"Чем пользуетесь?","options":["Телеграм","телеграм","Почта"]}'
    assert ca.clean_poll(raw) == {"question": "Чем пользуетесь?",
                                  "options": ["Телеграм", "Почта"]}
    assert ca.clean_poll("мусор") is None
