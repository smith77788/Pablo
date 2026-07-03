# Database — База данных

## Stack
PostgreSQL · asyncpg pool (min=15, max=50) · schema_v1 — schema_v126

## Таблицы

### tg_accounts
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| session_str | TEXT | StringSession |
| phone | TEXT | Телефон |
| is_active | BOOLEAN | Активен |
| acc_status | TEXT | active/cooldown/banned/deactivated |
| trust_score | FLOAT | 0-1 |
| proxy_id | BIGINT | FK → user_proxies |

### operation_queue
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| op_type | TEXT | Тип операции |
| status | TEXT | pending/running/done/failed/cancelled |
| params | JSONB | Параметры |
| total_items/done_items | INT | Прогресс |
| result | JSONB | Результат |

### operation_audit
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| operation_id | BIGINT | FK → operation_queue |
| action | TEXT | Действие |
| result | TEXT | success/error |

### managed_channels / managed_bots
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL | PK |
| owner_id | BIGINT | Владелец |
| channel_id/bot_id | BIGINT | Telegram ID |

## Индексы
```sql
CREATE INDEX idx_accounts_owner ON tg_accounts(owner_id);
CREATE INDEX idx_queue_status ON operation_queue(status);
CREATE INDEX idx_audit_account ON operation_audit(account_id);
```

## Миграции
- Файлы `schema_v*.sql` в корне
- Идемпотентны (IF NOT EXISTS)
- Только ADD COLUMN, не удалять
