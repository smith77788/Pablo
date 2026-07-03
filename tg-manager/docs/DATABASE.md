# Database — База данных

## Stack

- PostgreSQL
- asyncpg connection pool (min=15, max=50)
- Schema migrations: `schema_v1.sql` — `schema_v126.sql`

---

## Основные таблицы

### tg_accounts

Telegram аккаунты пользователей.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец (telegram user_id) |
| session_str | TEXT | Telethon StringSession |
| phone | TEXT | Номер телефона |
| first_name | TEXT | Имя |
| username | TEXT | @username |
| is_active | BOOLEAN | Активен ли |
| acc_status | TEXT | Статус: active/cooldown/banned/deactivated |
| trust_score | FLOAT | Уровень доверия 0-1 |
| proxy_id | BIGINT | FK → user_proxies |
| tags | TEXT[] | Теги |
| pool | TEXT | Пул аккаунтов |

### managed_channels

Управляемые каналы.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| channel_id | BIGINT | Telegram channel ID |
| title | TEXT | Название |
| username | TEXT | @username |
| acc_id | BIGINT | FK → tg_accounts |
| type | TEXT | Тип: channel/group |

### managed_bots

Управляемые боты.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| bot_id | BIGINT | Telegram bot ID |
| token | TEXT | Токен (зашифрован) |
| username | TEXT | @username |
| first_name | TEXT | Имя |
| added_by | BIGINT | FK → platform_users |

### operation_queue

Очередь операций.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| op_type | TEXT | Тип операции |
| status | TEXT | Статус: pending/running/done/failed/cancelled |
| params | JSONB | Параметры |
| total_items | INT | Всего элементов |
| done_items | INT | Выполнено |
| retry_count | INT | Количество повторов |
| max_retries | INT | Максимум повторов |
| result | JSONB | Результат |
| error_msg | TEXT | Сообщение об ошибке |
| label | TEXT | Человекочитаемая метка |

### operation_audit

Аудит операций.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| operation_id | BIGINT | FK → operation_queue |
| account_id | BIGINT | FK → tg_accounts |
| action | TEXT | Действие |
| target | TEXT | Цель |
| result | TEXT | Результат: success/error |
| error_msg | TEXT | Сообщение об ошибке |
| occurred_at | TIMESTAMPTZ | Время |

---

## Экосистемы

### ecosystems

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| name | TEXT | Название |
| ecosystem_type | TEXT | Тип |
| status | TEXT | Статус |

### ecosystem_members

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| ecosystem_id | BIGINT | FK → ecosystems |
| object_type | TEXT | Тип: channel/bot/account |
| object_id | BIGINT | ID объекта |

---

## Индексы

```sql
-- Основные индексы
CREATE INDEX idx_tg_accounts_owner ON tg_accounts(owner_id);
CREATE INDEX idx_managed_channels_owner ON managed_channels(owner_id);
CREATE INDEX idx_operation_queue_status ON operation_queue(status);
CREATE INDEX idx_operation_queue_owner ON operation_queue(owner_id);
CREATE INDEX idx_operation_audit_account ON operation_audit(account_id);
```

---

## Миграции

Файлы `schema_v*.sql` в корне проекта.

### Порядок

1. Запускать по порядку (v1 → v2 → ... → v126)
2. Каждый файл идемпотентен (IF NOT EXISTS)
3. Не удалять колонки (только ADD COLUMN)

### Создание новой миграции

```sql
-- schema_v127.sql
ALTER TABLE table_name ADD COLUMN IF NOT EXISTS new_column TYPE;
```
