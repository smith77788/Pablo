# services/contacts_hub

Модуль **Unified Contacts Hub** — централизованное хранилище и движок управления
контактами из всех Telegram-аккаунтов пользователя. Агрегирует контакты из
нескольких сессий в единый граф (`unified_contacts`), обеспечивает поиск,
слияние дубликатов, версионирование, CRM, экспорт и AI-ассистент.

## Архитектура

```
contacts_hub/
├── repository.py           # CRUD-операции с unified_contacts
├── sync_service.py         # Синхронизация контактов из Telegram-аккаунтов
├── search_engine.py        # Поиск с операторами, fuzzy-совпадениями, кэшированием
├── merge_engine.py         # Обнаружение и слияние дубликатов
├── trust_engine.py         # Trust-скоринг, конфликты, smart-дубликаты
├── versioning_engine.py    # Версионирование с возможностью отката
├── relationship_engine.py  # Граф связей между контактами
├── identity_engine.py      # Identity graph (phone, email, username, tg_account)
├── stats_engine.py         # Агрегированная статистика
├── crm_engine.py           # CRM-данные: стадии, сделки, напоминания
├── smart_tags_engine.py    # Автоматические теги по правилам
├── bulk_ops_engine.py      # Массовые операции (теги, группы, удаление, мёрж)
├── export_engine.py        # Экспорт в CSV / VCF / JSON
├── ai_assistant.py         # Natural language query → результат
└── __init__.py             # Публичный API модуля
```

## Описание engine-файлов

### repository.py
Базовый CRUD: `get_contacts` (с фильтрами по тегу, группе, favorite, premium,
multi, mutual, account), `get_contact` (с sources, history, groups),
`upsert_contact` (ON CONFLICT по `owner_id + telegram_user_id`), `update_contact`,
`delete_contact`, группы (`get_contact_groups`, `add/remove_contact_to_group`),
статистика, логирование истории и синхронизации.

### sync_service.py
`sync_account` — подключается к Telethon-сессии аккаунта, получает контакты через
`GetContactsRequest`, upsert-ит в `unformed_contacts`, логирует sync. Обрабатывает
`AUTH_KEY_DUPLICATED` (помечает сессию как expired). `sync_all_accounts` —
параллельная синхронизация с `Semaphore(6)`, пер-аккаунт детализация и агрегация.

### search_engine.py
Полноценный поисковый движок: операторы `@username`, `#tag`, `company:...`,
`phone:...`; fuzzy-matching (Levenshtein + SequenceMatcher, порог 0.6);
LRU-кэш с TTL 300с; три режима: `search_contacts` (основной),
`search_contacts_spotlight` (быстрый type-ahead), `search_contacts_advanced`
(полная пагинация + fuzzy-расширение).

### merge_engine.py
`find_duplicates` — поиск по `telegram_user_id`; `auto_merge` — объединяет
с `MIN(id)` как primary; `manual_merge` — ручной выбор primary/secondary;
`merge_contacts_improved` — улучшенная версия с валидацией; `get_merge_preview` —
预览 объединённого результата; `auto_merge_with_confidence` — мёрж по порогу
confidence (0.7 по умолчанию).

### trust_engine.py
`compute_trust_score` — score на основе полей и sources (0.5–1.0);
`compute_merge_confidence` — confidence мёржа (telegram_id=0.5, phone=0.3,
username=0.25, name=0.15, company=0.1); `detect_smart_duplicates` /
`detect_smart_duplicates_improved` — парный O(n²) scan с source-overlap бонусом;
`update_trust_scores` — batch обновление; `get_conflicts` / `resolve_conflict` —
управление конфликтами; `get_conflict_resolution_suggestions` — авто-предложения.

### versioning_engine.py
`create_version` — снимок снэпшота в `contact_versions`;
`get_versions` / `get_version_detail` — список и детали;
`rollback_to_version` — откат по `version_num` с восстановлением всех полей;
`compare_versions` / `get_version_diff` — дифф двух версий.

### relationship_engine.py
`compute_relationships` — вычисляет связи: `phone_match` (0.95),
`same_username_pattern` (0.7), `same_company` (0.6), `mutual_account` (0.3);
сохраняет в `contact_relationships`. `get_relationships` — связи конкретного
контакта. `get_graph_stats` — общая статистика графа. `add_relationship` /
`remove_relationship` — ручное управление.

### identity_engine.py
`build_identity_graph` — собирает все идентификаторы контакта (telegram_id,
username, phones, emails, websites, tg_accounts) в `contact_identity_graph`.
`get_identity_graph` — чтение графа. `get_last_active` — относительное время
последней активности ("Был сегодня", "Был 3 дн. назад" и т.д.).
`add_identity` / `remove_identity` — редактирование. `compute_digital_footprint` —
score на основе каналов и sources.

### stats_engine.py
`get_full_stats` — делегирует в `repository.get_contact_stats`;
`get_account_stats` — количество контактов по каждому аккаунту.

### crm_engine.py
`get_crm_data` / `upsert_crm` — CRM-данные контакта (stage, deal_value,
currency, reminders, custom_fields). `log_crm_activity` / `get_crm_activity` —
журнал активностей. `get_upcoming_reminders` / `get_crm_overdue` — напоминания.
`get_crm_stats` — сводка по стадиям. `get_crm_pipeline` — воронка сделок.
`create_crm_activity` / `update_crm_stage` / `get_crm_reminders`.

### smart_tags_engine.py
7 встроенных правил (Premium, has_phone, has_email, multi_account, has_notes,
favorite, has_company). `_check_condition` — eq, neq, not_empty, gte, lte,
contains, regex. `apply_smart_tags` — прогоняет все правила по контактам.
`get_smart_tags` / `get_smart_tag_rules` — чтение. `create_smart_tag_rule` /
`delete_smart_tag_rule` / `toggle_smart_tag_rule` — управление кастомными правилами.

### bulk_ops_engine.py
Массовые операции: `bulk_tag` / `bulk_untag`, `bulk_set_favorite`,
`bulk_delete`, `bulk_add_to_group` / `bulk_remove_from_group`,
`bulk_merge`, `bulk_export`, `bulk_add_tags` / `bulk_remove_tags`,
`bulk_set_importance` / `bulk_set_rating`. Группы: `create_group`,
`update_group`, `delete_group`.

### export_engine.py
`export_csv` — полный CSV с заголовками. `export_vcard` — vCard 3.0 для
одного контакта. `export_vcf` — VCF-архив для списка контактов.
`export_json` — JSON с десериализацией jsonb-полей. `export_csv_streaming` —
потоковый CSV с keyset-пагинацией. `get_export_stats` — сводка перед экспортом.

### ai_assistant.py
`process_ai_query` — natural language → результат. Поддерживает паттерны:
"найди контакты с телефоном", "сколько контактов", "покажи дубликаты",
"контакты в группе Клиенты", "напоминания", "статистика", "избранные",
"мульти-аккаунт", "по компании", "синхронизация".

## API эндпоинты

Все эндпоинты доступны через Mini App API (`/api/miniapp/uch/...`).

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| GET | `/uch/contacts` | Список контактов (фильтры: search, tag, favorite, premium, multi, mutual) |
| GET | `/uch/contacts/{contact_id}` | Детали контакта (с sources, history, groups) |
| POST | `/uch/contacts/{contact_id}` | Обновление контакта |
| DELETE | `/uch/contacts/{contact_id}` | Удаление контакта |
| GET | `/uch/search?q=...` | Поиск контактов |
| GET | `/uch/spotlight?q=...` | Быстрый поиск (type-ahead) |
| GET | `/uch/stats` | Статистика контактов |
| POST | `/uch/sync` | Синхронизация всех аккаунтов |
| GET | `/uch/groups` | Список групп |
| POST | `/uch/groups` | Создание группы |
| PUT | `/uch/groups/{group_id}` | Обновление группы |
| DELETE | `/uch/groups/{group_id}` | Удаление группы |
| POST | `/uch/contacts/{contact_id}/favorite` | Toggle избранного |
| POST | `/uch/contacts/{contact_id}/groups/{group_id}` | Добавить в группу |
| DELETE | `/uch/contacts/{contact_id}/groups/{group_id}` | Убрать из группы |
| GET | `/uch/contacts/{contact_id}/history` | История изменений |
| GET | `/uch/contacts/{contact_id}/versions` | Версии контакта |
| POST | `/uch/contacts/{contact_id}/rollback/{version_num}` | Откат к версии |
| GET | `/uch/contacts/{contact_id}/timeline` | Таймлайн активности |
| GET | `/uch/contacts/{contact_id}/relationships` | Связи контакта |
| GET | `/uch/contacts/{contact_id}/identity` | Identity graph |
| GET | `/uch/contacts/{contact_id}/crm` | CRM-данные + активности |
| POST | `/uch/contacts/{contact_id}/crm` | Upsert CRM-данных |
| POST | `/uch/contacts/{contact_id}/crm/activity` | Добавить CRM-активность |
| POST | `/uch/contacts/{contact_id}/crm/reminder` | Установить напоминание |
| POST | `/uch/duplicates` | Обнаружение дубликатов |
| POST | `/uch/merge` | Слияние двух контактов |
| GET | `/uch/conflicts` | Список конфликтов |
| POST | `/uch/conflicts/{conflict_id}` | Разрешение конфликта |
| GET | `/uch/smart-tags` | Smart-теги |
| GET | `/uch/smart-tag-rules` | Правила тегирования |
| POST | `/uch/smart-tags/apply` | Применить smart-теги |
| POST | `/uch/smart-tag-rules` | Создать правило |
| DELETE | `/uch/smart-tag-rules/{rule_id}` | Удалить правило |
| POST | `/uch/smart-tag-rules/{rule_id}/toggle` | Включить/выключить правило |
| POST | `/uch/bulk/tag` | Массовое добавление тега |
| POST | `/uch/bulk/untag` | Массовое удаление тега |
| POST | `/uch/bulk/favorite` | Массовое избранное |
| POST | `/uch/bulk/delete` | Массовое удаление |
| POST | `/uch/bulk/group` | Массовое добавление/удаление из группы |
| POST | `/uch/bulk/merge` | Массовое слияние |
| POST | `/uch/bulk/export` | Массовый экспорт |
| POST | `/uch/bulk/importance` | Массовая важность |
| POST | `/uch/bulk/rating` | Массовый рейтинг |
| GET | `/uch/export/csv` | Экспорт CSV |
| GET | `/uch/export/vcf` | Экспорт VCF |
| GET | `/uch/export/json` | Экспорт JSON |
| GET | `/uch/reminders` | Ближайшие + просроченные напоминания |
| GET | `/uch/graph/stats` | Статистика графа связей |
| POST | `/uch/graph/compute` | Пересчёт связей |
| POST | `/uch/trust/update` | Обновление trust-скоров |
| POST | `/uch/ai` | AI-запрос (natural language) |

## Примеры использования

```python
from services.contacts_hub import (
    get_contacts, search_contacts, sync_all_accounts,
    find_duplicates, auto_merge, detect_smart_duplicates,
    create_version, rollback_to_version, compute_relationships,
    export_csv, export_vcf, export_json, process_ai_query,
)

# Список контактов с фильтрами
result = await get_contacts(pool, owner_id, search='Иван',
                            favorite_only=True, tag='vip', limit=50)

# Поиск с операторами
results = await search_contacts(pool, owner_id, '@ivanov #клиент company:Яндекс')

# Синхронизация всех аккаунтов
sync_result = await sync_all_accounts(pool, owner_id)
print(f"Синхронизировано: {sync_result['total_synced']}, создано: {sync_result['total_created']}")

# Обнаружение и слияние дубликатов
dupes = await detect_smart_duplicates(pool, owner_id)
if dupes:
    await auto_merge(pool, owner_id)

# Версионирование
version_id = await create_version(pool, contact_id, owner_id, snapshot, changed_by='user')
await rollback_to_version(pool, contact_id, owner_id, version_num=3)

# Граф связей
rels = await compute_relationships(pool, owner_id, contact_id)

# Экспорт
csv_data = await export_csv(pool, owner_id)
vcf_data = await export_vcf(pool, owner_id)
json_data = await export_json(pool, owner_id)

# AI-ассистент
answer = await process_ai_query(pool, owner_id, "Сколько контактов с телефоном?")
# → {'answer': '📱 Контактов с телефоном: 142', 'contacts': [...], 'type': 'list'}
```

## Зависимости

- `asyncpg` — PostgreSQL-пул
- `services.account_manager` — Telethon-клиент для синхронизации
- `database.db` — получение сессий аккаунтов
