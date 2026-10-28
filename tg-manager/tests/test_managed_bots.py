"""Юнит-тесты Manager Mode: валидация username и сборка ссылки создания."""
from __future__ import annotations

import pytest

from services import managed_bots as mb


def test_validate_bot_username_ok():
    ok, norm = mb.validate_bot_username("my_helper_bot")
    assert ok and norm == "my_helper_bot"
    # @ и регистр
    ok, norm = mb.validate_bot_username("@My_Cool_Bot")
    assert ok and norm == "My_Cool_Bot"


def test_validate_bot_username_bad():
    assert mb.validate_bot_username("")[0] is False
    assert mb.validate_bot_username("short")[0] is False        # не на bot
    assert mb.validate_bot_username("helper")[0] is False        # не на bot
    assert mb.validate_bot_username("1startbot")[0] is False     # начинается с цифры
    assert mb.validate_bot_username("bad-name-bot")[0] is False  # дефис
    assert mb.validate_bot_username("a" * 40 + "bot")[0] is False  # длинно
    # ровно на границе снизу (5 символов, кончается bot)
    ok, norm = mb.validate_bot_username("a1bot")
    assert ok and norm == "a1bot"


def test_build_creation_link():
    link = mb.build_creation_link("ManagerBot", "my_helper_bot", "Мой Помощник")
    assert link.startswith("https://t.me/newbot/ManagerBot/my_helper_bot?name=")
    # имя URL-кодировано (кириллица + пробел)
    assert "%20" in link or "+" in link or "%D0" in link
    # без имени — просто путь без query
    link2 = mb.build_creation_link("@ManagerBot", "my_helper_bot")
    assert link2 == "https://t.me/newbot/ManagerBot/my_helper_bot"


def test_build_creation_link_errors():
    with pytest.raises(ValueError):
        mb.build_creation_link("", "my_helper_bot")       # нет менеджера
    with pytest.raises(ValueError):
        mb.build_creation_link("ManagerBot", "notvalid")  # плохой username
