from __future__ import annotations
import asyncio
import json
import logging
import re
import time

log = logging.getLogger(__name__)

# Сколько аккаунтов синхронизируем параллельно. Раньше sync_all_accounts обходил
# аккаунты СТРОГО последовательно, а каждый мёртвый/медленный аккаунт держит
# соединение до _CONNECT_TIMEOUT (~30с). При 5+ аккаунтах суммарное время
# превышало таймаут HTTP-шлюза → запрос падал 502/timeout, и пользователь видел
# «контакты не синхронизируются / модуль не работает». Ограниченная
# параллельность режет wall-time на порядок, не создавая всплеска соединений.
_SYNC_CONCURRENCY = 6


def classify_session_error(emsg: str) -> tuple[str, str]:
    """Классифицировать ошибку Telethon в понятную русскую причину + acc_status.

    Возвращает (friendly_ru, status): status ∈ {'dead','expired','flood','net',''}.
    'dead' — сессия необратимо инвалидирована (deactivated/удалён) → аккаунт
    деактивируем. 'expired' — ключ не признан сервером (AuthKeyUnregistered,
    revoked): нужен релог, но НЕ деактивируем жёстко — при системном сбое (напр.
    релей маршрутит не на тот DC) все аккаунты падают одинаково, и глушить их все
    нельзя. 'net' — временная транспортная/сетевая проблема, включая конфликт двух
    IP (AUTH_KEY_DUPLICATED): НЕ деактивируем, аккаунт живой. '' — прочее.

    Сырой английский текст исключения Telethon пользователю показывать нельзя —
    именно это утекало на экран «Синхронизация контактов — детали по аккаунтам».
    """
    low = (emsg or "").lower()
    if "auth_key_duplicated" in low or "two different ip" in low:
        # НЕ 'dead': конфликт двух IP временный (та же сессия секунду шла с двух
        # адресов — аккаунт кратко онлайн на телефоне/другом устройстве или дёрнулся
        # прокси). Многосессионность у Telegram штатная — телефон сам по себе не
        # конфликтует. Аккаунт живой: остудить и повторить, НЕ деактивировать.
        return ("сессия кратко конфликтовала (использовалась с двух IP одновременно) — "
                "аккаунт НЕ отключён. Так бывает, если ЭТА ЖЕ сессия активна где-то ещё "
                "(телефон/другой софт) или у аккаунта нет стабильного прокси. Дайте "
                "аккаунту свой прокси; если нужен отдельный от телефона сеанс — "
                "переавторизуйте здесь (он спокойно сосуществует с телефоном)", "net")
    if "deactivated" in low:
        return ("аккаунт удалён/деактивирован Telegram — восстановление невозможно",
                "dead")
    if ("authorization key" in low or "auth_key_unregistered" in low
            or "doesn't know about the authorization" in low
            or "session_revoked" in low or "session revoked" in low
            or "session_expired" in low or "session has expired" in low
            or "auth key" in low):
        return ("сессия недействительна — Telegram не знает этот ключ авторизации. "
                "Нужен релог аккаунта в разделе «Аккаунты». Если аккаунт работает "
                "через CF-релей — проверьте, что пул задеплоен с актуальными "
                "настройками (передеплойте пул).", "expired")
    if "flood" in low:
        return ("временное ограничение Telegram (flood wait) — повторите позже",
                "flood")
    if "proxy" in low or "connect" in low or "timeout" in low or "network" in low:
        return ("не удалось подключиться (прокси/сеть) — проверьте прокси аккаунта",
                "net")
    return (f"ошибка: {(emsg or '')[:140]}", "")


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
        # Второй источник — собеседники из личных диалогов. Адресная книга у
        # «рабочих» аккаунтов часто пуста, а переписок много: без этого система
        # «не обнаруживала» контакты, которых у аккаунта фактически десятки.
        # Сбой сбора диалогов НЕ должен ронять уже полученную адресную книгу.
        try:
            # limit=3000: у «рабочего» аккаунта личных переписок бывают тысячи —
            # дефолт 500 обрезал сбор, и флот отдавал лишь малую часть контактов
            # (жалоба «2.9к вместо 10к+»). Адресная книга собирается отдельно и
            # полностью (GetContactsRequest).
            dialog_contacts = await account_manager.get_dialog_contacts(
                acc['session_str'], limit=3000, _acc=dict(acc))
        except Exception as _de:
            log.warning('sync_account: сбор диалогов не удался acc=%s: %s', account_id, _de)
            dialog_contacts = []
        # Слияние по user_id: адресная книга приоритетнее (там есть is_mutual и,
        # как правило, телефон), диалоги лишь ДОБАВЛЯЮТ тех, кого в книге нет.
        _by_uid = {c['user_id']: c for c in dialog_contacts}
        for c in contacts:
            _by_uid[c['user_id']] = c  # книга перекрывает диалог для того же uid
        contacts = list(_by_uid.values())
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
            is_verified = bool(c.get('is_verified'))
            is_mutual = bool(c.get('is_mutual'))
            reg_est = c.get('registered_estimate')  # 'YYYY-MM-DD' | None
            last_seen_type = c.get('last_seen_type')
            last_seen_at = c.get('last_seen_at')  # ISO str | None
            # Полный сырой снимок всех полей — в digital_footprint (не теряем данные,
            # даже если под них ещё нет отдельной колонки).
            footprint = json.dumps({k: c.get(k) for k in (
                'is_scam', 'is_fake', 'is_restricted', 'access_hash',
                'last_seen_type', 'last_seen_at', 'registered_estimate',
            )})

            existing = await pool.fetchrow(
                'SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2',
                owner_id, user_id)
            if existing:
                await pool.execute(
                    'UPDATE unified_contacts SET username=$1, first_name=$2, last_name=$3, display_name=$4, '
                    'phones=$5::jsonb, is_premium=$6, is_verified=$7, is_mutual=$8, '
                    # ::text::date / ::text::timestamptz: reg_est и last_seen_at приходят
                    # ISO-СТРОКАМИ. Голый ::date заставлял asyncpg кодировать параметр
                    # как date и звать .toordinal() у str → падало на КАЖДОМ контакте
                    # («'str' object has no attribute 'toordinal'»). Приводим текст →
                    # тип уже в Postgres.
                    'registered_estimate=$9::text::date, last_seen_type=$10, '
                    'last_seen_at=$11::text::timestamptz, digital_footprint=$12::jsonb, '
                    'last_synced_at=NOW(), updated_at=NOW() WHERE id=$13',
                    username, first_name, last_name, display_name,
                    json.dumps(phones), is_premium, is_verified, is_mutual,
                    reg_est, last_seen_type, last_seen_at, footprint, existing['id'])
                contact_id = existing['id']
                updated += 1
            else:
                contact_id = str(__import__('uuid').uuid4())
                await pool.execute(
                    'INSERT INTO unified_contacts (id, owner_id, telegram_user_id, username, first_name, '
                    'last_name, display_name, phones, is_premium, is_verified, is_mutual, '
                    'registered_estimate, last_seen_type, last_seen_at, digital_footprint, last_synced_at) '
                    # $12/$14 — ISO-строки даты/времени: приводим text→тип в Postgres,
                    # иначе asyncpg зовёт .toordinal()/.timestamp() у str и падает.
                    'VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12::text::date,$13,$14::text::timestamptz,$15::jsonb,NOW())',
                    contact_id, owner_id, user_id, username, first_name,
                    last_name, display_name, json.dumps(phones), is_premium,
                    is_verified, is_mutual, reg_est, last_seen_type, last_seen_at, footprint)
                created += 1
            # Привязать ЭТОТ аккаунт как источник — и для НОВОГО, и для уже
            # существующего контакта. Раньше связь писалась только в ветке нового:
            # контакт, впервые добавленный ДРУГИМ аккаунтом, не числился за этим →
            # «по аккаунтам» показывало доли реального (Bella 792 вместо тысяч),
            # хотя контакты собраны. UNIQUE(contact_id, account_id) → идемпотентно.
            await pool.execute(
                'INSERT INTO contact_sources (contact_id, account_id, local_name, last_synced_at) '
                'VALUES ($1,$2,$3,NOW()) '
                'ON CONFLICT (contact_id, account_id) DO UPDATE SET last_synced_at=NOW()',
                contact_id, account_id, display_name)

        duration_ms = int((time.monotonic() - started) * 1000)
        await log_sync(pool, owner_id, account_id, 'auto', len(contacts), created, updated, 0, duration_ms)
        return {'synced': len(contacts), 'created': created, 'updated': updated}
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        emsg = str(e)
        log.warning('contacts_hub sync_account failed acc=%s: %s', account_id, emsg)
        # Классифицируем в понятную русскую причину — сырой английский текст
        # Telethon (напр. AuthKeyUnregistered «server doesn't know about the
        # authorization key») пользователю показывать нельзя.
        friendly, status = classify_session_error(emsg)
        # ВАЖНО: пометку acc_status здесь НЕ делаем. Решение о пометке принимает
        # sync_all_accounts, который видит КАРТИНУ по флоту: когда все аккаунты
        # падают одинаково — это системный сбой транспорта (сеть/прокси/релей/
        # IPv6), а не 28 одновременно протухших сессий, и метить их session_expired
        # (тем более только что залогиненные) — ложная тревога.
        raw = re.sub(r'\s+', ' ', emsg).strip()[:160]
        await log_sync(pool, owner_id, account_id, 'auto', 0, 0, 0, 0, duration_ms, friendly[:200])
        return {'error': friendly, 'status': status, 'raw': raw, 'synced': 0}


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
    failed = [(a, r) for a, r in zip(accounts, results) if r.get('error')]

    # Системный сбой vs изолированные проблемы. Когда почти ВЕСЬ флот падает
    # одинаково — это транспорт (сеть/прокси/релей/IPv6), а не 28 одновременно
    # протухших сессий. В этом случае НЕ метим аккаунты session_expired (ложная
    # тревога, особенно для только что залогиненных) и говорим об этом честно.
    systemic = accounts_found >= 3 and len(failed) >= max(3, int(accounts_found * 0.8))

    if not systemic:
        # Изолированные сбои — метим конкретные аккаунты (сигнал в «Аккаунты»).
        for acc, res in failed:
            st = res.get('status')
            fr = (res.get('error') or '')[:200]
            try:
                if st == 'dead':
                    await pool.execute(
                        "UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired', "
                        "status_reason=$2 WHERE id=$1", acc['id'], fr)
                elif st == 'expired':
                    await pool.execute(
                        "UPDATE tg_accounts SET acc_status='session_expired', "
                        "status_reason=$2 WHERE id=$1", acc['id'], fr)
            except Exception:
                pass

    # Честное сообщение: доминирующая РЕАЛЬНАЯ причина + сырой пример для диагностики,
    # а не гадание «сессии устарели».
    message = None
    if total_synced == 0:
        if not errors:
            message = 'Синхронизация прошла: у аккаунтов нет контактов для импорта.'
        else:
            raw_sample = next((r.get('raw') for _, r in failed if r.get('raw')), '')
            raw_tail = f' Пример ошибки: {raw_sample}' if raw_sample else ''
            if systemic:
                message = (
                    f'Похоже на системный сбой ПОДКЛЮЧЕНИЯ, а не сессий: {len(failed)} из '
                    f'{accounts_found} аккаунтов упали одинаково. Сессии, скорее всего, '
                    'в порядке (аккаунты только что вошли) — проблема в транспорте '
                    '(сеть/прокси/CF-релей/IPv6). Аккаунты НЕ помечены на релог.' + raw_tail)
            else:
                message = ('Аккаунты найдены, но контакты не получены — см. детали по '
                           'аккаунтам ниже.' + raw_tail)
    return {
        'accounts_found': accounts_found,
        'accounts_synced': len(results),
        'total_synced': total_synced,
        'total_created': total_created,
        'total_updated': total_updated,
        'errors': errors,
        'systemic': systemic,
        'details': details,
        'message': message,
    }
