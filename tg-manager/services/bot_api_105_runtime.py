"""Telegram 10.5 event boundaries, independent of the installed SDK models."""
from __future__ import annotations


def callback_edit_target(callback: dict) -> dict:
    """Return Telegram's actual message address, never a guessed recipient."""
    inline_id = callback.get('inline_message_id')
    if isinstance(inline_id, str) and inline_id:
        return {'inline_message_id': inline_id}
    message = callback.get('message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    message_id = message.get('message_id')
    if (isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id != 0
            and isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0):
        return {'chat_id': chat_id, 'message_id': message_id}
    return {}


def invited_recipient(message: dict) -> tuple[dict, dict] | None:
    """A private write-access service event identifies the recipient, not inviter."""
    access = message.get('write_access_allowed')
    chat = message.get('chat') or {}
    person = message.get('from') or {}
    if not isinstance(access, dict) or not isinstance(access.get('inviter'), dict):
        return None
    uid = chat.get('id')
    if (chat.get('type') != 'private' or not isinstance(uid, int)
            or isinstance(uid, bool) or uid <= 0):
        return None
    # Service-message sender may differ from its recipient. Never copy that
    # sender's identity into the private-chat recipient's audience record.
    if person.get('id') != uid:
        person = chat
    elif person.get('is_bot'):
        return None
    inviter = access['inviter']
    inviter_id = inviter.get('id')
    if (not isinstance(inviter_id, int) or isinstance(inviter_id, bool)
            or inviter_id <= 0 or inviter.get('is_bot')):
        return None
    identity = {'user_id': uid}
    for key in ('username', 'first_name', 'last_name', 'language_code'):
        value = person.get(key)
        if isinstance(value, str):
            identity[key] = value
    return identity, {'source': 'owner_invite', 'inviter_id': inviter_id}
