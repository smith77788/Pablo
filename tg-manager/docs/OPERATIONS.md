# Operations — Операции

## Типы операций

### Mass Operations

| Операция | Описание | Параметры |
|----------|----------|-----------|
| `mass_publish` | Публикация в каналы | text, channel_ids, delay |
| `bulk_join` | Вступление в каналы | links, account_ids, delay_mode |
| `bulk_leave` | Выход из каналов | channels, account_ids, delay_mode |
| `bulk_bot_edit` | Редактирование ботов | field, value, bot_ids |
| `bulk_post_to_channel` | Публикация в канал | channel_id, text, media |
| `bulk_chan_exec` | Редактирование каналов | field, value, channel_ids |
| `bulk_set_profile` | Установка профилей | profiles, account_ids |

### Creation Operations

| Операция | Описание | Параметры |
|----------|----------|-----------|
| `create_channel` | Создание канала | title, about, account_id |
| `create_group` | Создание группы | title, about, account_id |
| `bot_factory` | Создание ботов | name_template, username_template, count |
| `global_presence_*` | Global Presence | plan_id, asset_type |

### Intelligence Operations

| Операция | Описание | Параметры |
|----------|----------|-----------|
| `scan_owned_resources` | Скан ресурсов | account_ids |
| `check_accounts_health` | Проверка здоровья | account_ids |
| `parse_audience` | Парсинг аудитории | source_ref, parse_type |
| `content_clone` | Клонирование | source_bot_id, target_bot_id, fields |

### Strike Operations

| Операция | Описание | Параметры |
|----------|----------|-----------|
| `strike` | Жалоба на контент | targets, accounts, preset, mode |
| `mass_report` | Массовая жалоба | targets, accounts, reason |
| `mass_invite` | Массовый инвайт | group, user_refs, phones, batch_size |

---

## Выполнение операций

### op_worker流程

```
1. op_worker.poll() → pending операции
2. _process_pending() → захват с учётом параллельности
3. _run_op_task() → выбор _exec_* функции
4. _exec_*() → выполнение с safe DB helpers
5. Progress Monitor → обновления каждые 15сек
6. Результат → UPDATE operation_queue SET status='done'/'failed'
7. Уведомление → Telegram + SSE
```

### Retry Logic

```
FloodWait → ожидание как Telegram просит + 60с jitter
PeerFlood → 48h cooldown + ротация аккаунта
AUTH_KEY dead → немедленная деактивация
CHANNEL_PRIVATE → пропуск канала
Other → exponential backoff ±20% jitter
```

### Circuit Breaker

```
3+ ошибки подряд → trip (30мин cooldown)
Cooldown истёк → reset
Успех → decay (1 ошибка убирается)
```

---

## Прогресс

### Monitor

- Проверяет каждые 15 секунд
- Отправляет update каждые 30 секунд
- Milestone уведомления: 25%, 50%, 75%
- ETA на основе скорости обработки
- Speed: items/minute

### Формат уведомления

```
⏳ Операция #123 — 45%
[█████░░░░░] 45/100
mass_publish 12/мин ⏰ ETA: ~5мин
```
