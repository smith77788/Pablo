"""10.5 callback addresses and owner-invited recipients through real runtime."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import User
from services import auto_responder as ar
from services import workflow_runtime as runtime
from services.bot_api_105_runtime import callback_edit_target, invited_recipient

from tests.test_auto_responder_poison_update import _FakePool


def test_get_me_full_info_fields_survive_existing_sdk_model():
    info = User.model_validate({'id': 555, 'is_bot': True, 'first_name': 'Бот',
        'active_usernames': ['one_bot', 'two_bot'], 'inline_query_placeholder': 'Поиск',
        'needs_inline_query_location': False, 'has_topic_sidebar': True})
    dumped = info.model_dump()
    assert dumped['active_usernames'] == ['one_bot', 'two_bot']
    assert dumped['needs_inline_query_location'] is False
    assert dumped['has_topic_sidebar'] is True


def test_inline_address_never_uses_the_clicking_user_as_destination():
    assert callback_edit_target({'inline_message_id': 'inline-id', 'from': {'id': 42}}) == {
        'inline_message_id': 'inline-id'}
    assert callback_edit_target({'from': {'id': 42}}) == {}
    assert callback_edit_target({'message': {'message_id': 9, 'chat': {'id': -100}}}) == {
        'chat_id': -100, 'message_id': 9}


def _invitation():
    return {'message_id': 8, 'chat': {'id': 7001, 'type': 'private'},
            'from': {'id': 7001, 'first_name': 'Гость', 'is_bot': False},
            'write_access_allowed': {'inviter': {'id': 42, 'first_name': 'Владелец', 'is_bot': False}}}


def test_invitation_distinguishes_the_recipient_from_the_inviter():
    person, source = invited_recipient(_invitation())
    assert person['user_id'] == 7001
    assert source == {'source': 'owner_invite', 'inviter_id': 42}
    assert 'first_name' not in source


@pytest.mark.parametrize('change', [
    {'chat': {'id': -100, 'type': 'group'}},
    {'from': {'id': 7001, 'is_bot': True}},
    {'from': {'id': 7001, 'is_bot': False}, 'write_access_allowed': None},
])
def test_invalid_invitation_cannot_add_an_unrelated_subscriber(change):
    message = _invitation()
    message.update(change)
    assert invited_recipient(message) is None


def test_invite_without_text_reaches_audience_and_event_but_not_auto_reply():
    upsert, emit, send = AsyncMock(), AsyncMock(), AsyncMock()
    async def go():
        with (patch('database.db.get_update_offset', AsyncMock(return_value=0)),
              patch('database.db.set_update_offset', AsyncMock()),
              patch('database.db.get_active_auto_replies', AsyncMock(return_value=[])),
              patch('database.db.get_active_funnels', AsyncMock(return_value=[])),
              patch('database.db.get_active_automation_rules', AsyncMock(return_value=[])),
              patch('database.db.get_active_experiment', AsyncMock(return_value=None)),
              patch('database.db.upsert_users', upsert),
              patch.object(ar, '_record_bot_ok', AsyncMock()),
              patch('services.brand_injection.is_free_tier', AsyncMock(return_value=False)),
              patch('services.virtual_layer.signal_for_telegram_user', AsyncMock()),
              patch.object(ar.automation_events, 'emit', emit),
              patch.object(ar.bot_api, 'send_message', send)):
            await ar._process_bot(_FakePool(), object(), 555, 'test_token',
                                  updates=[{'update_id': 1, 'message': _invitation()}])
    asyncio.run(go())
    assert upsert.await_args.args[2][0]['user_id'] == 7001
    assert emit.await_args.args[1:3] == (1, 'contact.write_access_allowed')
    assert emit.await_args.kwargs['chat_id'] == 7001
    send.assert_not_awaited()


def test_workflow_inline_callback_removes_its_own_inline_keyboard():
    class Pool:
        async def fetchrow(self, query, *args):
            assert args[1:3] == (555, 7001)
            return {'id': 8, 'owner_id': 1, 'current_step': 0,
                    'steps_snapshot': [{'buttons': [{'text': 'Да'}]}]}
        async def execute(self, *args):
            return 'UPDATE 1'
    call = AsyncMock(return_value={'ok': True})
    async def go():
        with (patch('services.bot_api._call', call), patch.object(runtime, '_log', AsyncMock()),
              patch.object(runtime.bus, 'emit', AsyncMock()), patch.object(runtime, 'wake')):
            await runtime.on_button(Pool(), object(), 'test_token', 555, 7001,
                                    {'data': 'wf:8:1:0', 'inline_message_id': 'inline-message'})
    asyncio.run(go())
    assert call.await_args.kwargs == {
        'inline_message_id': 'inline-message', 'reply_markup': {'inline_keyboard': []}}


def test_service_sender_does_not_overwrite_private_recipient_identity():
    message = _invitation()
    message['from'] = {'id': 42, 'first_name': 'Владелец', 'is_bot': False}
    message['chat']['first_name'] = 'Получатель'
    recipient, _source = invited_recipient(message)
    assert recipient == {'user_id': 7001, 'first_name': 'Получатель'}
