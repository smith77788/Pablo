"""Паритет: карточка аккаунта в боте умеет Story и апелляцию спамблока.

Раньше post_story / appeal_spamblock вызывались только из mini-app
(account_post_story / account_spamblock_appeal). Бэкенды (story_manager.post_story,
account_manager.appeal_spamblock) уже есть — здесь проверяем бот-сторону:
кнопки в карточке, хендлеры, FSM Story (media→caption) и корректные вызовы.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_account_card_has_story_and_appeal_buttons():
    h = _read("bot/handlers/accounts.py")
    assert 'AccCb(action="post_story"' in h
    assert 'AccCb(action="spamblock_appeal"' in h


def test_handlers_registered():
    h = _read("bot/handlers/accounts.py")
    assert 'AccCb.filter(F.action == "post_story")' in h
    assert 'AccCb.filter(F.action == "spamblock_appeal")' in h


def test_story_fsm_two_steps_media_then_caption():
    h = _read("bot/handlers/accounts.py")
    assert "class AccountStory" in h
    assert "AccountStory.waiting_media" in h
    assert "AccountStory.waiting_caption" in h
    # медиа обязано быть прямой ссылкой (валидация)
    assert 'startswith(("http://", "https://"))' in h


def test_handlers_call_real_backends():
    h = _read("bot/handlers/accounts.py")
    assert "account_manager.appeal_spamblock(acc[\"session_str\"], dict(acc))" in h
    assert "story_manager.post_story(" in h


def test_backends_exist():
    assert "async def appeal_spamblock(" in _read("services/account_manager.py")
    assert "async def post_story(" in _read("services/story_manager.py")
