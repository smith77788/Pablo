# Database — База данных

## Stack
PostgreSQL · asyncpg pool (min=15, max=50) · `schema.sql` + `schema_v2.sql` …
`schema_v193_channel_ownership.sql` (194 файла на 2026-09-03, число растёт —
см. «Миграции» ниже и `docs/SCHEMA_CONSOLIDATION_PLAN.md` про свёртку истории
в baseline без большого разового риска).

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
- Применяются `database/db.py::create_pool()` на старте процесса, в порядке
  версии, **с пропуском уже применённых**: таблица `schema_migrations` хранит
  статус каждого файла, и файлы со статусом `ok` при следующем старте не
  выполняются. Перезапускаются только файлы со статусом `warnings` (был
  частичный сбой) и те, которых в журнале ещё нет.
- Транзакция на файл, savepoint на оператор: сбой одного оператора не рвёт весь
  файл, а фиксируется в `schema_migrations.last_error`.
- `SET lock_timeout` (по умолчанию `5s`, переопределяется `SCHEMA_LOCK_TIMEOUT`):
  DDL, не получивший блокировку, честно падает и повторяется, но **приложение
  стартует в любом случае**. Иначе `ALTER TABLE`, ждущий блокировку за старым
  контейнером, вешал бы каждый запрос к данным.
- После миграций проверяются критичные таблицы. По умолчанию их отсутствие —
  ERROR в лог (падать нельзя: получился бы цикл перезапусков); `SCHEMA_STRICT=1`
  превращает это в отказ старта.
- Baseline (`schema_baseline.sql` + `schema_baseline.manifest`): на **заведомо
  чистой** базе — пустой журнал И нет `tg_accounts` — применяется снимок схемы,
  а покрытые им файлы истории помечаются применёнными. Снимок генерируется из
  живой базы: `deploy/scripts/make_schema_baseline.py`. У существующего
  окружения baseline не берётся никогда.
- Идемпотентны (IF NOT EXISTS)
- Только ADD COLUMN, не удалять (в 194 файлах на 2026-09-03 нет ни одного
  `DROP TABLE`/`DROP COLUMN`)
- Число файлов нарушает `.botmother/21_DATABASE_GOVERNANCE.md`
  ("avoid uncontrolled schema growth"). Свёртка — по фазам, не разом:
  `docs/SCHEMA_CONSOLIDATION_PLAN.md`.
