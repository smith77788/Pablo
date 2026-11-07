"""Личные почтовые подключения: проверка SMTP без отправки письма."""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl

from aiohttp import web

from services.token_vault import encrypt_token

log = logging.getLogger(__name__)

PROVIDERS = {
    'gmail': ('Gmail', 'smtp.gmail.com', 587),
    'outlook': ('Outlook / Microsoft 365', 'smtp-mail.outlook.com', 587),
    'yandex': ('Яндекс', 'smtp.yandex.ru', 465),
    'mailru': ('Mail.ru', 'smtp.mail.ru', 465),
    'yahoo': ('Yahoo', 'smtp.mail.yahoo.com', 587),
    'icloud': ('iCloud', 'smtp.mail.me.com', 587),
}


def check_credentials(host: str, port: int, email: str, password: str) -> None:
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as server:
            server.login(email, password)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email, password)


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp

    # Не позволяем проверкам SMTP занять весь пул потоков приложения.
    slots = asyncio.Semaphore(3)

    async def mailbox_list(request):
        uid = _get_uid(request)
        if not uid:
            return _err('Unauthorized', 401)
        rows = await pool.fetch(
            'SELECT id, email, is_active, fail_count, auth_type '
            'FROM strike_email_accounts WHERE owner_id=$1 ORDER BY id', uid)
        from services import email_oauth
        return _json_resp({
            'mailboxes': [dict(row) for row in rows],
            'providers': [{'key': key, 'label': value[0]} for key, value in PROVIDERS.items()],
            'oauth': [{'key': 'google', 'label': 'Войти через Google',
                       'available': email_oauth.is_provider_configured('google')}],
        })

    async def mailbox_oauth_start(request):
        uid = _get_uid(request)
        if not uid:
            return _err('Unauthorized', 401)
        from services import email_oauth
        provider = request.match_info.get('provider', '')
        if provider != 'google':
            return _err('Этот провайдер быстрого входа пока не поддерживается', 400)
        if not email_oauth.is_provider_configured(provider):
            return _err('Вход через Google ещё не настроен администратором приложения', 503)
        try:
            url = email_oauth.build_miniapp_auth_url(uid, provider)
        except ValueError:
            log.warning('mailbox oauth configuration invalid owner=%s provider=%s', uid, provider)
            return _err('Настройки быстрого входа некорректны. Обратитесь к администратору.', 503)
        return _json_resp({'url': url})

    async def mailbox_connect(request):
        uid = _get_uid(request)
        if not uid:
            return _err('Unauthorized', 401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            email = str(body.get('email') or '').strip().lower()
            password = body.get('password')
            provider = PROVIDERS.get(str(body.get('provider') or ''))
            if not provider or not re.fullmatch(r'[^\s<>@\x00-\x1f]+@[^\s<>@]+\.[^\s<>@]+', email):
                raise ValueError
            if len(email) > 254 or not isinstance(password, str) or not 4 <= len(password) <= 1024:
                raise ValueError
        except (ValueError, TypeError):
            return _err('Проверьте почтового провайдера, адрес ящика и пароль приложения', 400)
        if slots.locked():
            return _err('Проверки почты заняты. Повторите через минуту.', 429)
        try:
            async with slots:
                await asyncio.wait_for(asyncio.to_thread(
                    check_credentials, provider[1], provider[2], email, password), 35)
        except smtplib.SMTPAuthenticationError:
            return _err('Почта отклонила вход. Проверьте пароль приложения. '
                        'Провайдер может требовать OAuth или разрешение SMTP.', 400)
        except (OSError, smtplib.SMTPException, TimeoutError) as exc:
            log.warning('mailbox connection failed owner=%s provider=%s kind=%s',
                        uid, body.get('provider'), type(exc).__name__)
            return _err('Не удалось подключиться к почте. Проверьте доступность SMTP. '
                        'Railway Free, Trial и Hobby блокируют исходящий SMTP.', 502)
        encrypted = encrypt_token(password)
        await pool.execute(
            """INSERT INTO strike_email_accounts
               (owner_id,email,smtp_host,smtp_port,smtp_pass,auth_type)
               VALUES($1,$2,$3,$4,$5,'password')
               ON CONFLICT(owner_id,email) DO UPDATE SET
               smtp_host=$3,smtp_port=$4,smtp_pass=$5,auth_type='password',
               oauth_provider=NULL,oauth_refresh_token=NULL,oauth_access_token=NULL,
               oauth_expires_at=NULL,oauth_scopes='{}'::text[],is_active=TRUE,fail_count=0""",
            uid, email, provider[1], provider[2], encrypted)
        return _json_resp({'ok': True, 'email': email})

    async def mailbox_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err('Unauthorized', 401)
        try:
            mailbox_id = int(request.match_info['mailbox_id'])
            if not 0 < mailbox_id < 2**63:
                raise ValueError
        except (KeyError, ValueError):
            return _err('Неверный ящик', 400)
        row = await pool.fetchrow(
            'DELETE FROM strike_email_accounts WHERE id=$1 AND owner_id=$2 RETURNING id',
            mailbox_id, uid)
        if not row:
            return _err('Ящик не найден', 404)
        return _json_resp({'ok': True})

    app.router.add_get("/api/miniapp/mailboxes", mailbox_list)
    app.router.add_post("/api/miniapp/mailboxes", mailbox_connect)
    app.router.add_delete("/api/miniapp/mailboxes/{mailbox_id}", mailbox_delete)
    app.router.add_post("/api/miniapp/mailboxes/oauth/{provider}", mailbox_oauth_start)
