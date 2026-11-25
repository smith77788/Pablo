"""Одинаковые двери в меню и безопасный вход в браузере для Telegram X."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers import botmother_menu as menu
from bot.handlers import start


def test_main_menu_has_native_and_compatible_browser_entry(monkeypatch):
    monkeypatch.setattr(menu, '_APP_URL', 'https://example.org/miniapp')
    buttons = [b for row in menu._main_menu_kb().inline_keyboard for b in row]
    assert any(b.web_app for b in buttons)
    fallback = next(b for b in buttons if b.text == '🌐 Telegram X / браузер')
    assert fallback.callback_data == 'bm:browser_login::0:0'
    assert fallback.web_app is None


def test_commands_share_menu_and_keep_returning_user_statistics():
    from pathlib import Path

    source = Path(start.__file__).read_text()
    main = source[source.index('async def cmd_start('):source.index('async def cb_help(')]
    assert 'await send_main_menu(message,' in main
    assert 'Нажмите на бота в списке' not in main
    assert '_onboarding_message(pool, uid)' in main
    assert 'start_param.startswith("pay_")' in main
    assert 'start_param == "about"' in main


def test_browser_callback_uses_clicking_owner_not_bot_message(monkeypatch):
    helper = AsyncMock()
    monkeypatch.setattr(start, 'send_browser_login', helper)
    monkeypatch.setattr(menu, 'safe_answer', AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(type='private'), from_user=SimpleNamespace(id=999))
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=42))
    pool = object()
    asyncio.run(menu.cb_browser_login(callback, pool))
    helper.assert_awaited_once_with(message, pool, 42)


def test_pair_code_and_url_are_separate(monkeypatch):
    import config
    from services import device_pairing

    monkeypatch.setattr(config, 'MINI_APP_URL', 'https://example.org/miniapp')
    create = AsyncMock(return_value='ABCD-EFGH')
    monkeypatch.setattr(device_pairing, 'create_pairing_code', create)
    message = SimpleNamespace(chat=SimpleNamespace(type='private'), answer=AsyncMock())
    pool = object()
    asyncio.run(start.send_browser_login(message, pool, 42))
    create.assert_awaited_once_with(pool, 42)
    reply = message.answer.await_args
    assert 'ABCD-EFGH' in reply.args[0]
    button = reply.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.url == 'https://example.org/miniapp/'
    assert button.web_app is None
    assert 'ABCD-EFGH' not in button.url


def test_group_pair_does_not_issue_or_publish_credentials(monkeypatch):
    from services import device_pairing

    create = AsyncMock()
    monkeypatch.setattr(device_pairing, 'create_pairing_code', create)
    message = SimpleNamespace(chat=SimpleNamespace(type='group'), answer=AsyncMock(), from_user=SimpleNamespace(id=42))
    asyncio.run(start.cmd_pair(message, object()))
    create.assert_not_awaited()
    assert 'личном чате' in message.answer.await_args.args[0]


def test_group_browser_callback_never_issues_code(monkeypatch):
    helper = AsyncMock()
    monkeypatch.setattr(start, 'send_browser_login', helper)
    monkeypatch.setattr(menu, 'safe_answer', AsyncMock())
    callback = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(type='supergroup')),
        from_user=SimpleNamespace(id=42), bot=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(menu.cb_browser_login(callback, object()))
    helper.assert_not_awaited()
    callback.bot.send_message.assert_not_awaited()
    assert '/pair' in menu.safe_answer.await_args.args[1]
    assert menu.safe_answer.await_args.kwargs['show_alert'] is True


def test_new_user_onboarding_also_has_compatible_entry(monkeypatch):
    import config
    from services import onboarding

    monkeypatch.setattr(config, 'MINI_APP_URL', 'https://example.org/miniapp')
    monkeypatch.setattr(onboarding, 'load_checklist', AsyncMock(return_value={
        'steps': [{'id': 'accounts', 'title': 'Аккаунт', 'done': False}],
        'next_step': 'accounts', 'done': 0, 'total': 1}))
    _, keyboard = asyncio.run(start._onboarding_message(object(), 42))
    assert any(button.callback_data == 'bm:browser_login::0:0'
               for row in keyboard.inline_keyboard for button in row)
