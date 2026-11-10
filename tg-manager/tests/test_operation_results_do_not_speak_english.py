"""Итог операции не показывает английские ответы Telegram.

ЧТО БЫЛО. Исполнители подставляют текст исключения в итог («Ошибка проверки:
{exc}»), а у Telethon он английский. В сводке частых сбоев первой строкой
стояло «Cannot find any entity corresponding to …» — владелец и его
пользователи английского не читают.

ЧТО ТЕПЕРЬ. Перевод стоит в единственной точке, через которую проходит итог
любой операции, и от памяти исполнителя не зависит.
"""
from __future__ import annotations

import pytest

from services import op_errors

CASES = [
    ('⚠️ Ошибка проверки: Cannot find any entity corresponding to "@nobody_here"',
     ["не найден адресат", "@nobody_here"], ["Cannot", "entity"]),
    ("⚠️ Прогрев не запущен: A wait of 381 seconds is required (caused by JoinChannelRequest)",
     ["подождать 381 с"], ["wait of", "caused by", "JoinChannelRequest"]),
    ("Ошибка: The channel specified is private and you lack permission to access it. "
     "Another reason may be that you were banned from it (caused by GetFullChannelRequest)",
     ["канал закрыт"], ["private", "lack permission", "caused by"]),
    ('No user has "ghost" as username', ["«ghost»", "никем не занято"], ["No user has"]),
    ("Chat admin privileges are required to do that in the specified chat (for example, "
     "to send a message in a channel which is not yours)",
     ["права администратора"], ["privileges"]),
    ("The key is not registered in the system (caused by GetDialogsRequest)",
     ["войдите в аккаунт заново"], ["registered"]),
]


@pytest.mark.parametrize("summary, must, must_not", CASES)
def test_a_known_telegram_answer_reaches_the_owner_in_russian(summary, must, must_not):
    res = op_errors._normalize_result(
        {"status": "failed", "summary": summary}, "reg_check", 1.0)
    for part in must:
        assert part in res["summary"], res["summary"]
    for part in must_not:
        assert part not in res["summary"], res["summary"]


def test_russian_text_and_names_are_left_alone():
    for text in ("✅ Опубликовано: 5, ошибок: 0",
                 "🔍 Ad Intel @durov_channel: найдено 3 рекламных постов",
                 "Готово"):
        res = op_errors._normalize_result({"status": "done", "summary": text}, "x", 0.1)
        assert res["summary"] == text


def test_an_unknown_reason_is_kept_rather_than_lost():
    text = "⚠️ Ошибка: Some brand new server answer"
    assert op_errors.russify_telegram_text(text) == text
    assert op_errors.russify_telegram_text(None) is None
    assert op_errors.russify_telegram_text({"a": 1}) == {"a": 1}
