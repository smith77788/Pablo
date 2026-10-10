from __future__ import annotations
import json
import logging
import re
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

QUERY_PATTERNS = [
    {'pattern': r'(?:найди|покажи|найти|показать)\s+(?:всех?\s+)?(?:контакт\w*\s+)?(?:у\s+)?(?:котор\w+\s+)?(?:есть|име\w+)\s+(.+)',
     'handler': 'search_field'},
    {'pattern': r'(?:сколько|кол-во|количество)\s+(?:контакт\w*|людей|люди)',
     'handler': 'count_all'},
    {'pattern': r'(?:сколько|кол-во)\s+(?:контакт\w*\s+)?(?:с|с)\s+(?:premium|премиум)',
     'handler': 'count_premium'},
    {'pattern': r'(?:контакт\w*\s+)?(?:без|нет)\s+(?:телефон\w*|номер\w*)',
     'handler': 'without_phone'},
    {'pattern': r'(?:контакт\w*\s+)?(?:с|с)\s+(?:телефон\w*|номер\w*)',
     'handler': 'with_phone'},
    {'pattern': r'(?:дубликат\w*|дубли)',
     'handler': 'find_duplicates'},
    {'pattern': r'(?:тег\w*)\s+(.+)',
     'handler': 'by_tag'},
    {'pattern': r'(?:групп\w*)\s+(.+)',
     'handler': 'by_group'},
    {'pattern': r'(?:избранн\w*|фаворит\w*)',
     'handler': 'favorites'},
    {'pattern': r'(?:мульти|несколько)\s+(?:аккаунт\w*|акк)',
     'handler': 'multi_account'},
    {'pattern': r'(?:напоминан\w*|ремайндер\w*)',
     'handler': 'reminders'},
    {'pattern': r'(?:кто\s+)?(?:в\s+)?(?:compan\w*|компани\w*)\s+(.+)',
     'handler': 'by_company'},
    {'pattern': r'(?:последн\w*\s+)?(?:синхрониз\w*|синхр)',
     'handler': 'sync_status'},
    {'pattern': r'(?:статистик\w*|стат\w*|цифры)',
     'handler': 'stats'},
]


def _match_query(text: str) -> Optional[dict]:
    text_lower = text.lower().strip()
    for pattern in QUERY_PATTERNS:
        m = re.search(pattern['pattern'], text_lower)
        if m:
            return {'handler': pattern['handler'], 'match': m, 'groups': m.groups()}
    return None


async def process_ai_query(pool, owner_id: int, query: str) -> dict:
    result = _match_query(query)
    if not result:
        return {
            'answer': 'Не удалось распознать запрос. Попробуйте:\n• "Найди контакты с телефоном"\n• "Сколько контактов"\n• "Покажи дубликаты"\n• "Контакты в группе Клиенты"\n• "Напоминания"',
            'contacts': [],
            'type': 'help',
        }

    handler = result['handler']
    groups = result['groups']

    try:
        if handler == 'count_all':
            total = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1', owner_id)
            return {'answer': f'📊 Всего контактов: {total}', 'contacts': [], 'type': 'count'}

        elif handler == 'count_premium':
            cnt = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_premium=TRUE', owner_id)
            return {'answer': f'💎 Контактов с Premium: {cnt}', 'contacts': [], 'type': 'count'}

        elif handler == 'without_phone':
            rows = await pool.fetch(
                "SELECT id, first_name, last_name, username FROM unified_contacts WHERE owner_id=$1 AND (phones = '[]'::jsonb OR phones IS NULL) ORDER BY first_name LIMIT 20",
                owner_id)
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'📱 Контактов без телефона: {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'with_phone':
            rows = await pool.fetch(
                "SELECT id, first_name, last_name, username FROM unified_contacts WHERE owner_id=$1 AND phones != '[]'::jsonb AND phones IS NOT NULL ORDER BY first_name LIMIT 20",
                owner_id)
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'📱 Контактов с телефоном: {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'find_duplicates':
            from services.contacts_hub.trust_engine import detect_smart_duplicates
            dupes = await detect_smart_duplicates(pool, owner_id)
            if not dupes:
                return {'answer': '✅ Дубликатов не найдено', 'contacts': [], 'type': 'list'}
            lines = [f"🔍 Найдено {len(dupes)} возможных дубликатов:"]
            for d in dupes[:10]:
                lines.append(f"• {d['contact_a']['name']} ↔ {d['contact_b']['name']} ({int(d['confidence']*100)}%)")
            return {'answer': '\n'.join(lines), 'contacts': [], 'type': 'duplicates'}

        elif handler == 'favorites':
            rows = await pool.fetch(
                "SELECT id, first_name, last_name, username FROM unified_contacts WHERE owner_id=$1 AND is_favorite=TRUE ORDER BY first_name LIMIT 20",
                owner_id)
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'⭐ Избранных контактов: {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'multi_account':
            rows = await pool.fetch(
                """SELECT uc.id, uc.first_name, uc.last_name, uc.username,
                   (SELECT COUNT(*) FROM contact_sources cs WHERE cs.contact_id = uc.id) as acc_count
                   FROM unified_contacts uc WHERE uc.owner_id=$1
                   AND (SELECT COUNT(*) FROM contact_sources cs WHERE cs.contact_id = uc.id) > 1
                   ORDER BY acc_count DESC LIMIT 20""",
                owner_id)
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?', 'extra': f"{r['acc_count']} акк."} for r in rows]
            return {'answer': f'👥 Мульти-аккаунт контактов: {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'reminders':
            from services.contacts_hub.crm_engine import get_upcoming_reminders, get_crm_overdue
            upcoming = await get_upcoming_reminders(pool, owner_id)
            overdue = await get_crm_overdue(pool, owner_id)
            lines = []
            if overdue:
                lines.append(f'⚠️ Просроченных: {len(overdue)}')
                for r in overdue[:5]:
                    lines.append(f'  • {r.get("contact_name", "?")} — {r.get("next_reminder_text", "")}')
            if upcoming:
                lines.append(f'⏰ Ближайших: {len(upcoming)}')
                for r in upcoming[:5]:
                    lines.append(f'  • {r.get("contact_name", "?")} — {r.get("next_reminder_text", "")}')
            if not lines:
                lines.append('✅ Нет напоминаний')
            return {'answer': '\n'.join(lines), 'contacts': [], 'type': 'reminders'}

        elif handler == 'by_tag':
            tag = groups[0].strip() if groups else ''
            rows = await pool.fetch(
                "SELECT id, first_name, last_name, username FROM unified_contacts WHERE owner_id=$1 AND $2 = ANY(tags) ORDER BY first_name LIMIT 20",
                owner_id, tag)
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'🏷 Контактов с тегом "{tag}": {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'by_group':
            group_name = groups[0].strip() if groups else ''
            rows = await pool.fetch(
                """SELECT uc.id, uc.first_name, uc.last_name, uc.username
                   FROM unified_contacts uc
                   JOIN contact_group_members cgm ON cgm.contact_id = uc.id
                   JOIN contact_groups cg ON cg.id = cgm.group_id
                   WHERE uc.owner_id=$1 AND cg.name ILIKE $2
                   ORDER BY uc.first_name LIMIT 20""",
                owner_id, f'%{group_name}%')
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'📂 Контактов в группе "{group_name}": {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'by_company':
            company = groups[0].strip() if groups else ''
            rows = await pool.fetch(
                "SELECT id, first_name, last_name, username FROM unified_contacts WHERE owner_id=$1 AND company ILIKE $2 ORDER BY first_name LIMIT 20",
                owner_id, f'%{company}%')
            contacts = [{'id': r['id'], 'name': f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r['username'] or '?'} for r in rows]
            return {'answer': f'🏢 Контактов в компании "{company}": {len(contacts)}', 'contacts': contacts, 'type': 'list'}

        elif handler == 'sync_status':
            stats = await pool.fetchrow(
                'SELECT MAX(last_synced_at) as last_sync, COUNT(*) as total FROM unified_contacts WHERE owner_id=$1',
                owner_id)
            last = stats['last_sync'] if stats and stats['last_sync'] else None
            total = stats['total'] if stats else 0
            last_str = last.strftime('%d.%m.%Y %H:%M') if last else 'никогда'
            return {'answer': f'🔄 Последняя синхронизация: {last_str}\n📊 Всего контактов: {total}', 'contacts': [], 'type': 'status'}

        elif handler == 'stats':
            from services.contacts_hub.repository import get_contact_stats
            s = await get_contact_stats(pool, owner_id)
            lines = [
                f'📊 Статистика контактов:',
                f'• Всего: {s["total"]}',
                f'• Premium: {s["premium"]}',
                f'• С username: {s["with_username"]}',
                f'• С телефоном: {s["with_phone"]}',
                f'• Избранные: {s["favorites"]}',
                f'• С заметками: {s["notes"]}',
                f'• Тегов: {s["tags"]}',
                f'• Групп: {s["groups"]}',
            ]
            return {'answer': '\n'.join(lines), 'contacts': [], 'type': 'stats'}

        else:
            return {'answer': 'Функция в разработке', 'contacts': [], 'type': 'unknown'}

    except Exception as e:
        log.warning("ai_query error owner=%d query='%s': %s", owner_id, query[:100], e)
        return {'answer': f'⚠️ Ошибка: {str(e)[:100]}', 'contacts': [], 'type': 'error'}
