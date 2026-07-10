# Database — База данных

## Stack
PostgreSQL · asyncpg pool (min=15, max=50) · `schema.sql` + `schema_v2.sql` … `schema_v152.sql`
(151 файлов на 2026-07-09, число растёт — см. «Миграции» ниже и
`docs/SCHEMA_CONSOLIDATION_PLAN.md` про план консолидации без большого
разового риска).

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
- Файлы `schema_v*.sql` в корне (+ дубли в `database/` при наличии)
- Применяются `database/db.py::create_pool()` **заново при каждом старте
  процесса** — нет пропуска уже применённых файлов, единственная защита от
  повторного применения — идемпотентность SQL (`IF NOT EXISTS` и т.п.).
  Таблица `schema_migrations` фиксирует статус применения каждого файла, но
  используется только для наблюдаемости, не для skip-логики.
- Идемпотентны (IF NOT EXISTS)
- Только ADD COLUMN, не удалять (в 151 файле на 2026-07-09 нет ни одного
  `DROP TABLE`/`DROP COLUMN`)
- Число файлов уже нарушает `.botmother/21_DATABASE_GOVERNANCE.md`
  ("avoid uncontrolled schema growth"). Не консолидировать всё разом —
  конкретный, дробимый на фазы план: `docs/SCHEMA_CONSOLIDATION_PLAN.md`.
