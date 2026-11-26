"""Native Bot API 10.5 models and manager HTTP connector contract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.__meta__ import __api_version__
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetMe, SetMySettings
from aiogram.types import BotFullInfo


def test_sdk_supports_botfullinfo_and_settings():
    assert tuple(map(int, __api_version__.split('.'))) >= (10, 5)
    assert GetMe.__returning__ is BotFullInfo
    payload = {key: False for key, field in BotFullInfo.model_fields.items() if field.is_required()}
    payload.update(id=42, is_bot=True, first_name='Bot', active_usernames=['bot_alias'],
                   inline_query_placeholder='Search', has_topic_sidebar=True)
    info = BotFullInfo.model_validate(payload)
    assert info.active_usernames == ['bot_alias'] and info.has_topic_sidebar
    assert SetMySettings(can_read_all_group_messages=False).model_dump(exclude_none=True)['can_read_all_group_messages'] is False


def test_manager_tls_connector_contract_survives_sdk_upgrade():
    session = AiohttpSession()
    assert isinstance(session._connector_init, dict)
    assert 'ssl' in session._connector_init
    asyncio.run(session.close())


def test_vault_has_working_bot_screen_without_mini_app(monkeypatch):
    import config
    from bot.callbacks import VaultCb
    from bot.handlers import business_vault as handler

    monkeypatch.setattr(config, 'MINI_APP_URL', '')
    monkeypatch.setattr(handler.vault, 'active_connection_for_owner', AsyncMock(return_value={'can_reply': True}))
    render = AsyncMock(return_value=('Список диалогов', SimpleNamespace(inline_keyboard=[])))
    monkeypatch.setattr(handler, 'render_dialog_list', render)
    message = SimpleNamespace(answer=AsyncMock(), bot=SimpleNamespace(get_me=AsyncMock(
        return_value=SimpleNamespace(username='demo_bot'))))
    pool = object()
    asyncio.run(handler.send_vault_screen(message, pool, 42))
    buttons = message.answer.await_args_list[0].kwargs['reply_markup'].inline_keyboard
    assert any(VaultCb.unpack(button.callback_data).a == 'list' for row in buttons for button in row)
    assert all(button.web_app is None for row in buttons for button in row)
    render.assert_awaited_once_with(pool, 42, 'all', 0)
    assert message.answer.await_args_list[1].args[0] == 'Список диалогов'
