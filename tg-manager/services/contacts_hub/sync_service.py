from __future__ import annotations
import asyncio
import json
import logging
import time

log = logging.getLogger(__name__)

# Сколько аккаунтов синхронизируем параллельно. Раньше sync_all_accounts обходил
# аккаунты СТРОГО последовательно, а каждый мёртвый/медленный аккаунт держит
# соединение до _CONNECT_TIMEOUT (~30с). При 5+ аккаунтах суммарное время
# превышало таймаут HTTP-шлюза → запрос падал 502/timeout, и пользователь видел
# «контакты не синхронизируются / модуль не работает». Ограниченная
# параллельность режет wall-time на порядок, не создавая всплеска соединений.
_SYNC_CONCURRENCY = 6


async def sync_account(pool, owner_id: int, account_id: int) -> dict:
    from database.db import get_account_for_telethon
    from services import account_manager
    from services.contacts_hub.repository import log_sync

    started = time.monotonic()
    acc = await get_account_for_telethon(pool, account_id, owner_id)
    if not acc or not acc.get('session_str'):
        return {'error': 'Session not found', 'synced': 0}

    try:
        # account_manager.get_contacts owns its own client connect/disconnect and
        # uses the real GetContactsRequest TL call — client.get_contacts() is not
        # a real Telethon method (this used to raise AttributeError on every sync).
        contacts = await account_manager.get_contacts(acc['session_str'], _acc=dict(acc))
        created = 0
        updated = 0
        for c in contacts:
            user_id = c['user_id']
            username = c.get('username') or None
            first_name = c.get('first_name') or ''
            last_name = c.get('last_name') or ''
            display_name = f"{first_name} {last_name}".strip()
            phones = [c['phone']] if c.get('phone') else []
            is_premium = bool(c.get('is_premium'))

            existing = await pool.fetchrow(
                'SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2',
                owner_id, user_id)
            if existing:
                await pool.execute(
                    'UPDATE unified_contacts SET username=$1, first_name=$2, last_name=$3, display_name=$4, '
                    'phones=$5::jsonb, is_premium=$6, last_synced_at=NOW(), updated_at=NOW() WHERE id=$7',
                    username, first_name, last_name, display_name,
                    json.dumps(phones), is_premium, existing['id'])
                updated += 1
            else:
                contact_id = str(__import__('uuid').uuid4())
                await pool.execute(
                    'INSERT INTO unified_contacts (id, owner_id, telegram_user_id, username, first_name, '
                    'last_name, display_name, phones, is_premium, last_synced_at) '
                    'VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,NOW())',
                    contact_id, owner_id, user_id, username, first_name,
                    last_name, display_name, json.dumps(phones), is_premium)
                await pool.execute(
                    'INSERT INTO contact_sources (contact_id, account_id, local_name, last_synced_at) '
                    'VALUES ($1,$2,$3,NOW()) ON CONFLICT DO NOTHING',
                    contact_id, account_id, display_name)
                created += 1

        duration_ms = int((time.monotonic() - started) * 1000)
        await log_sync(pool, owner_id, account_id, 'auto', len(contacts), created, updated, 0, duration_ms)
        return {'synced': len(contacts), 'created': created, 'updated': updated}
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning('contacts_hub sync_account failed acc=%s: %s', account_id, e)
        await log_sync(pool, owner_id, account_id, 'auto', 0, 0, 0, 0, duration_ms, str(e)[:200])
        return {'error': str(e)[:200], 'synced': 0}


async def sync_all_accounts(pool, owner_id: int) -> dict:
    # НЕ фильтруем по is_active: аккаунт мог быть деактивирован health-проверкой,
    # но его сессия всё ещё валидна для ЧТЕНИЯ контактов (безопасная операция).
    # Раньше is_active=TRUE отсекал такие аккаунты → пользователь видел их в
    # разделе «Аккаунты», а контакты показывали «0» → «модуль не работает».
    accounts = await pool.fetch(
        "SELECT id, phone, first_name, is_active FROM tg_accounts "
        "WHERE owner_id=$1 AND session_str IS NOT NULL AND session_str <> ''",
        owner_id)
    accounts_found = len(accounts)
    if not accounts_found:
        # Отдельно считаем аккаунты вообще (в т.ч. без сессии), чтобы отличить
        # «нет аккаунтов» от «есть, но без сохранённой сессии».
        any_acc = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", owner_id) or 0
        msg = ('Нет аккаунтов с сохранённой сессией. Подключите аккаунт в разделе '
               '«Аккаунты» (вход по номеру телефона), затем синхронизируйте контакты.'
               if any_acc == 0 else
               f'Найдено аккаунтов: {any_acc}, но ни у одного нет сохранённой сессии '
               '— переавторизуйте их в разделе «Аккаунты».')
        return {
            'accounts_found': 0,
            'accounts_synced': 0,
            'total_synced': 0,
            'total_created': 0,
            'total_updated': 0,
            'errors': [],
            'details': [],
            'message': msg,
        }

    sem = asyncio.Semaphore(_SYNC_CONCURRENCY)
    by_id = {a['id']: a for a in accounts}

    async def _one(acc_id: int) -> dict:
        async with sem:
            try:
                return await sync_account(pool, owner_id, acc_id)
            except Exception as e:  # sync_account сам ловит, но страхуемся от gather-обрыва
                log.warning('sync_all_accounts: acc=%s crashed: %s', acc_id, e)
                return {'error': str(e)[:200], 'synced': 0}

    results = await asyncio.gather(*[_one(a['id']) for a in accounts])

    # Пер-аккаунт детализация — чтобы пользователь (и мы) видели, ЧТО именно
    # произошло с каждым аккаунтом, а не только агрегат.
    details = []
    for acc, res in zip(accounts, results):
        label = acc['first_name'] or acc['phone'] or f"#{acc['id']}"
        if res.get('error'):
            details.append({'account': label, 'ok': False, 'reason': res['error']})
        else:
            details.append({'account': label, 'ok': True, 'synced': res.get('synced', 0)})

    total_synced = sum(r.get('synced', 0) for r in results)
    total_created = sum(r.get('created', 0) for r in results)
    total_updated = sum(r.get('updated', 0) for r in results)
    errors = [r.get('error') for r in results if r.get('error')]
    message = None
    if total_synced == 0:
        if errors:
            message = ('Аккаунты найдены, но контакты не получены. Вероятно, '
                       'сессии устарели или прокси недоступны — проверьте детали ниже.')
        else:
            message = 'Синхронизация прошла: у аккаунтов нет контактов для импорта.'
    return {
        'accounts_found': accounts_found,
        'accounts_synced': len(results),
        'total_synced': total_synced,
        'total_created': total_created,
        'total_updated': total_updated,
        'errors': errors,
        'details': details,
        'message': message,
    }
