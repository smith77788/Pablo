"""Быстрый вход в почту: привязка владельца, браузера и шифрование токенов."""
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from services import email_oauth as oauth
from services import mini_app_api, mini_app_mailboxes
from services.token_vault import decrypt_token, encrypt_token


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        'EMAIL_OAUTH_STATE_SECRET': 'test-state-secret',
        'TOKEN_ENCRYPTION_KEY': 'test-encryption-key',
        'GOOGLE_OAUTH_CLIENT_ID': 'test-client',
        'GOOGLE_OAUTH_CLIENT_SECRET': 'test-client-secret',
        'EMAIL_OAUTH_REDIRECT_URI': 'https://example.org/oauth/email/callback',
    }.items():
        monkeypatch.setenv(key, value)


def test_browser_handoff_has_unique_signed_state_and_pkce(configured):
    url = oauth.build_miniapp_auth_url(42, 'google')
    state = parse_qs(urlsplit(url).query)['state'][0]
    assert oauth.parse_state(state)['owner_id'] == 42
    provider = parse_qs(urlsplit(oauth._provider_auth_url('google', state)).query)
    assert provider['code_challenge_method'] == ['S256']
    expected = oauth._b64(hashlib.sha256(oauth._pkce_verifier(state).encode()).digest())
    assert provider['code_challenge'] == [expected]
    assert oauth.make_state(42, 'google') != oauth.make_state(42, 'google')
    raw, signature = state.split('.')
    with pytest.raises(ValueError):
        oauth.parse_state(raw + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:])


@pytest.mark.asyncio
async def test_callback_refuses_other_browser(configured, monkeypatch):
    exchange = AsyncMock(return_value='me@example.org')
    monkeypatch.setattr(oauth, 'exchange_code_and_save', exchange)
    app = web.Application()
    oauth.add_routes(app, object(), SimpleNamespace(send_message=AsyncMock()))
    handler = next(r.handler for r in app.router.routes()
                   if r.method == 'GET' and r.resource.get_info().get('path') == '/oauth/email/callback')
    state = oauth.make_state(42, 'google', flow='miniapp')
    request = SimpleNamespace(query={'code':'code', 'state':state}, cookies={})
    response = await handler(request)
    assert response.status == 400
    exchange.assert_not_called()
    request.cookies['ig_email_oauth'] = hashlib.sha256(state.encode()).hexdigest()
    response = await handler(request)
    assert response.status == 200
    assert exchange.call_args.args[1:3] == (42, 'google')
    assert exchange.call_args.kwargs['code_verifier'] == oauth._pkce_verifier(state)


@pytest.mark.asyncio
async def test_exchange_stores_only_encrypted_tokens(configured, monkeypatch):
    monkeypatch.setattr(oauth, '_request_token', AsyncMock(return_value={
        'access_token':'access-secret', 'refresh_token':'refresh-secret',
        'scope':'https://mail.google.com/', 'expires_in':3600,
    }))
    monkeypatch.setattr(oauth, '_fetch_profile', AsyncMock(return_value={
        'email':'me@example.org', 'verified_email':True,
    }))
    pool = SimpleNamespace(execute=AsyncMock())
    assert await oauth.exchange_code_and_save(pool, 42, 'google', 'code') == 'me@example.org'
    args = pool.execute.call_args.args
    assert args[1] == 42
    assert 'access-secret' not in args and 'refresh-secret' not in args
    assert decrypt_token(args[6]) == 'refresh-secret'
    assert decrypt_token(args[7]) == 'access-secret'


@pytest.mark.asyncio
async def test_cached_encrypted_access_token_is_decrypted(configured):
    mailbox = dict(oauth_provider='google', oauth_access_token=encrypt_token('access'),
                   oauth_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert await oauth.get_access_token(None, mailbox) == 'access'


@pytest.mark.asyncio
async def test_oauth_start_uses_authenticated_owner(configured, monkeypatch):
    monkeypatch.setattr(mini_app_api, '_get_uid', lambda request: 42)
    app = web.Application()
    mini_app_mailboxes.setup_routes(app, object())
    handler = next(r.handler for r in app.router.routes()
                   if r.method == 'POST' and r.resource.get_info().get('formatter')
                   == '/api/miniapp/mailboxes/oauth/{provider}')
    response = await handler(SimpleNamespace(match_info={'provider':'google'}))
    state = parse_qs(urlsplit(json.loads(response.text)['url']).query)['state'][0]
    assert oauth.parse_state(state)['owner_id'] == 42


@pytest.mark.asyncio
async def test_missing_config_is_actionable_not_gateway_error(configured, monkeypatch):
    monkeypatch.delenv('GOOGLE_OAUTH_CLIENT_ID')
    monkeypatch.setattr(mini_app_api, '_get_uid', lambda request: 42)
    app = web.Application()
    mini_app_mailboxes.setup_routes(app, object())
    handler = next(r.handler for r in app.router.routes()
                   if r.method == 'POST' and r.resource.get_info().get('formatter')
                   == '/api/miniapp/mailboxes/oauth/{provider}')
    response = await handler(SimpleNamespace(match_info={'provider':'google'}))
    assert response.status == 409
    assert 'GOOGLE_OAUTH_CLIENT_ID' in response.text
    assert 'test-client-secret' not in response.text


def test_browser_uses_navigation_not_the_telegram_stub():
    node = shutil.which('node')
    assert node, 'Для проверки нужен Node.js'
    source = Path(__file__).resolve().parents[1] / 'mini_app/screens/mailboxes.js'
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
let navigation = '', external = '';
global.window = {location:{assign:url => navigation=url}};
global.tg = {initData:'', openLink:()=>{throw Error('called stub');}};
openMailboxOAuthUrl('https://example.org/start');
if(navigation !== 'https://example.org/start') throw Error('no navigation');
tg = {initData:'signed-data', openLink:url=>external=url};
openMailboxOAuthUrl('https://example.org/telegram');
if(external !== 'https://example.org/telegram') throw Error('no external link');
'''
    result = subprocess.run([node, '-e', script, str(source)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_oauth_click_opens_synchronously_and_errors_stay_visible():
    node = shutil.which('node')
    assert node, 'Для проверки нужен Node.js'
    source = Path(__file__).resolve().parents[1] / 'mini_app/screens/mailboxes.js'
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
function element(tag) {return {tag,style:{},children:[],setAttribute(){},
  replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)},
  insertBefore(x,y){this.children.splice(this.children.indexOf(y),0,x)}};}
global.document={createElement:element};
let requests=0, opened='', fail=false;
global.api=async()=>{requests++;if(fail)throw Error('Нет настроек');
  return {url:'https://example.org/oauth/email/start?state=test'};};
global.errRu=e=>e.message;
global.tg={initData:'signed',openLink:url=>opened=url};
(async()=>{
  const box=element('div'), provider={key:'google',label:'Войти через Google'};
  await prepareMailboxOAuth(provider,box);
  const link=box.children.find(x=>x.tag==='a');
  if(!link || link.disabled)throw Error('no clickable link');
  const result=link.onclick({preventDefault(){}});
  if(result instanceof Promise || !opened || requests!==1)throw Error('async click');
  fail=true;await prepareMailboxOAuth(provider,box);
  if(!box.children.some(x=>x.textContent==='Нет настроек'))throw Error('hidden error');
  if(!box.children.some(x=>x.tag==='button' && x.onclick))throw Error('no retry');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script, str(source)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
