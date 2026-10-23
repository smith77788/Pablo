# Automation Workflows

## Назначение

Модуль автоматизации массовых операций и бизнес-процессов в Telegram. Включает систему согласования опасных операций, автономное планирование execution-шагов и автоматические ответы на действия пользователей.

## Основные функции

- **Approval workflow** — двухфакторное подтверждение опасных массовых операций
- **Autonomous planning** — автономное планирование execution-стратегий и управление рисками
- **Auto-responders** — автоматические ответы на определённые действия/триггеры
- **Очередь операций** — управление статусами операций (pending, running, waiting_approval)
- **RBAC и контроль доступа** — ограничение прав для опасных операций

## API эндпоинты

### Approval Workflow

```python
# bot/handlers/approval_flow.py

@router.callback_query(ApprovalCb.filter(F.action == "confirm"))
async def cb_approval_confirm(callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool):
    """Подтверждение операции. Переводит статус из waiting_approval в pending."""

@router.callback_query(ApprovalCb.filter(F.action == "cancel"))
async def cb_approval_cancel(callback: CallbackQuery, callback_data: ApprovalCb, pool: asyncpg.Pool):
    """Отмена операции. Устанавливает статус cancelled."""
```

### Autonomous Engine

```python
# services/autonomous_engine.py

class AutonomousContract:
    """Контракт автономного планирования.
    
    Поля:
        intent_type: тип намерения
        description: описание задачи
        strategy: стратегия execution (safest, balanced, fastest, scalable)
        plan: план выполнения
        forecast: прогноз ресурсов
        resource_plan: план ресурсов
        queue_plan: план очереди
        risk_plan: план управления рисками
        recovery_plan: план восстановления
        execution_plan: список шагов execution
    """
```

### Управление статусами операций

```python
# Типичные SQL-запросы для управления статусами:

# Перевод в статус waiting_approval
UPDATE operation_queue 
SET status='waiting_approval', requires_approval=TRUE 
WHERE id=$1;

# Подтверждение операции
UPDATE operation_queue 
SET requires_approval=FALSE, approved_at=now(), approved_by=$1, status='pending' 
WHERE id=$2 AND status='waiting_approval';

# Отмена операции
UPDATE operation_queue 
SET status='cancelled' 
WHERE id=$1 AND status='waiting_approval';
```

## Примеры использования

```python
# Пример создания операции с требованием согласования
await pool.execute(
    """INSERT INTO operation_queue 
       (owner_id, op_type, total_items, status, requires_approval)
       VALUES ($1, $2, $3, 'waiting_approval', TRUE)""",
    owner_id, 'mass_invite', 1000
)

# Проверка статуса операции
op = await pool.fetchrow(
    "SELECT status, requires_approval FROM operation_queue WHERE id=$1",
    op_id
)
if op['status'] == 'waiting_approval':
    # Показать клавиатуру подтверждения
    kb = _approval_kb(op_id)

# Автономное планирование стратегии
contract = AutonomousContract(
    intent_type='mass_invite',
    description='Массовый инвайт в группу',
    strategy='balanced',
    plan={...},
    forecast={...},
    resource_plan={...},
    queue_plan={...},
    risk_plan={...},
    recovery_plan={...},
    execution_steps=['select_accounts', 'warm_up', 'execute_invites']
)
```

## Конфигурация безопасных операций

Операции, требующие согласования:
- `mass_invite` — массовый инвайт > 100 пользователей
- `mass_broadcast` — массовая рассылка > 500 получателей
- `mass_report` — массовые жалобы > 50
- `account_reset` — сброс настроек аккаунта

Стратегии execution:
- `safest` — минимальные риски, максимальные задержки
- `balanced` — компромисс между скоростью и безопасностью
- `fastest` — максимальная скорость, повышенные риски
- `scalable` — масштабируемая стратегия для больших объёмов

## Интеграция с другими модулями

- **Flood Engine** — учёт FloodWait при планировании
- **Resource Selector** — выбор аккаунтов для execution
- **Strike Engine** — учёт жалоб при принятии решений
- **Account Manager** — проверка состояния аккаунтов перед запуском