"""Официальные маршруты не превращаются в рассылку по всем адресам."""
import pytest
from services import abuse_report as ar
from services import report_channels as channels


def test_category_recipient_and_store_forms():
    assert channels.email_recipient('copyright') == 'dmca@telegram.org'
    assert channels.email_recipient('csam', 'child') == 'stopCA@telegram.org'
    with pytest.raises(ValueError):
        channels.email_recipient('scam', 'child')
    for key in ('apple', 'google'):
        entry = next(c for c in channels.for_category('scam') if c['key'] == key)
        assert entry['kind'] == 'manual'
        assert entry['source'].startswith('https://')
        with pytest.raises(ValueError):
            channels.email_recipient('scam', key)


@pytest.mark.asyncio
async def test_selected_mailbox_always_scoped_to_owner():
    class Pool:
        async def fetchrow(self, sql, *args):
            assert 'owner_id=$1' in sql and 'id=$2' in sql
            assert 'is_active=TRUE' in sql
            assert args == (42, 7)
            return None
    assert await ar._primary_mailbox(Pool(), 42, 7) is None


@pytest.mark.asyncio
async def test_default_send_decrypts_password_and_keeps_original(monkeypatch):
    from services import strike_engine, token_vault

    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', 'mailbox-regression-test-key')
    encrypted = token_vault.encrypt_token('plain-secret')
    async def send(*args, **kwargs):
        assert args[3] == 'plain-secret'
        return True, ''
    monkeypatch.setattr(strike_engine, '_send_email', send)
    mailbox = dict(email='me@example.org', smtp_host='smtp.example.org',
                   smtp_port=587, smtp_pass=encrypted, auth_type='password')
    assert await ar._default_send(None, mailbox, ['abuse@telegram.org'], 'title', 'body') == (True, '')
    assert mailbox['smtp_pass'] == encrypted


@pytest.mark.asyncio
async def test_unreadable_password_never_reaches_smtp(monkeypatch):
    from services import token_vault

    monkeypatch.setattr(token_vault, 'decrypt_token', lambda value: value)
    mailbox = dict(smtp_pass='ENC:broken', auth_type='password')
    ok, error = await ar._default_send(None, mailbox, ['abuse@telegram.org'], 'x', 'y')
    assert not ok and 'Переподключите' in error
