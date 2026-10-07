# 🔴 КРИТИЧЕСКИЕ АРХИТЕКТУРНЫЕ ПРОБЛЕМЫ

Дата: 2026-05-28
Статус: **ТРЕБУЮТ ПЕРЕРАБОТКИ**

---

## ПРОБЛЕМА 1: Управление пользователями и подписками (КРИТИЧ)

### Текущее состояние ❌
- Новые пользователи приходят но НЕ записываются в учёт
- Нет таблицы `platform_users` или она не используется
- **Невозможно** выдать/забрать подписку из админ-панели
- Нет списка всех клиентов с фильтром по плану
- Нет быстрого действия (1 нажатие = выдать на 1мес)

### Требуемое решение ✅
1. **Таблица `platform_users`** ОБЯЗАТЕЛЬНА:
   ```sql
   user_id BIGINT PRIMARY KEY
   username TEXT
   first_name TEXT
   registered_at TIMESTAMPTZ DEFAULT now()
   last_seen TIMESTAMPTZ
   current_plan TEXT (free/starter/pro/enterprise)
   plan_expires_at TIMESTAMPTZ
   is_banned BOOLEAN DEFAULT false
   ```

2. **Модуль `admin_users.py`** — новый хендлер:
   - Меню: `/admin` → **👥 Пользователи**
   - Список всех пользователей с пагинацией
   - Фильтр: по плану, по дате регистрации, по статусу
   - Кнопка на юзере: **Выдать план** (выпадающее меню 1/3/6/12 мес)
   - Кнопка: **Забрать подписку** (instant)
   - Кнопка: **Забанить** (блокировка доступа)
   - История действий админа над юзером

3. **Callback `AdminUserCb`** (prefix="admu"):
   - action=list, user_id, plan, months
   - action=grant_plan
   - action=revoke_plan
   - action=ban_user

---

## ПРОБЛЕМА 2: "Message is not modified" — должно исправляться в коде, не пропускаться (КРИТ)

### Текущее состояние ❌
- В главном error handler добавлена тихая перехват
- Это скрывает BUG, а не исправляет его

### Требуемое решение ✅
- КАЖДЫЙ `callback.message.edit_text()` должен:
  ```python
  if callback.message and callback.message.text != new_text:
      await callback.message.edit_text(new_text, ...)
  else:
      await callback.answer("✅ Уже актуально", show_alert=False)
  ```
- Либо в утилите `op_helpers.py`:
  ```python
  async def safe_edit_text(callback, text, markup=None):
      if callback.message and callback.message.text != text:
          await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
      else:
          await callback.answer("✅")
  ```

---

## ПРОБЛЕМА 3: AI Assistant не выполняет реальные задачи (КРИТ)

### Текущее состояние ❌
```
Пользователь: Создай реальное присутствие из 50 каналов в разных столицах с описанием, ссылками, командами
AI: "К сожалению, я не умею это делать. Вот рекомендации..."
```
- AI Assistant только дает рекомендации (chat-интерфейс)
- **НЕ выполняет** реальные операции

### Требуемое решение ✅
**Разделение интерфейсов:**

1. **🤖 AI Recommendations** (text-chat, текущее)
   - Когда пользователь спрашивает совет
   - Выводит рекомендации через Claude API

2. **🔧 AI Operations Manager** (NEW) — выполняет реальные операции
   - Пользователь говорит: "Создай 50 каналов в городах..."
   - AI-агент ПОНИМАЕТ задачу и:
     - Создаёт операцию `global_presence_channel`
     - Заполняет параметры (города, имена, описания)
     - **Запускает в operation_queue**
   - Пользователь видит: "⚙️ Операция #123 запущена: создание 50 каналов"

**Реализация:**
```python
# bot/handlers/ai_operations.py (NEW)
async def cb_ai_execute(callback, state, pool):
    """
    1. Получить задачу от пользователя (FSM)
    2. Через Claude API распарсить: что нужно сделать, параметры
    3. Перевести в operation_queue задачу
    4. Выполнить через op_worker
    """
```

---

## ПРОБЛЕМА 4: Global Presence не соблюдает Anti-ban правила (КРИТ)

### Текущее состояние ❌
- ❌ Не использует `session_simulator` для задержек
- ❌ Не проверяет trust_score аккаунта перед операцией
- ❌ Не меняет тайминги в зависимости от времени суток
- ❌ Не учитывает flood_wait от Telegram

### Требуемое решение ✅
**В `op_worker.py` функция `_exec_global_presence_channel()`:**

```python
# ДО создания канала
for i, target in enumerate(targets):
    acc = acc_by_id[target["selected_account_id"]]
    
    # 1. Проверить trust_score
    trust = await pool.fetchval(
        "SELECT trust_score FROM tg_accounts WHERE id=$1", acc["id"]
    )
    if trust < 0.3:
        # Пропустить аккаунт, выбрать другой
        acc = get_account_with_highest_trust(...)
    
    # 2. Умные тайминги
    delay = session_simulator.smart_batch_delay(i, batch_size=10)
    delay *= session_simulator.time_of_day_factor()  # ночь: 2-5x
    await asyncio.sleep(delay)
    
    # 3. Симуляция поведения
    await session_simulator.typing_delay("New Channel Name")
    
    # 4. Создание с catch flood_wait
    result = await account_manager.create_channel(...)
    if result.get("flood_wait"):
        wait = int(result["flood_wait"]) + 15
        await asyncio.sleep(wait)
        result = retry(...)  # повторить
```

---

## ПРОБЛЕМА 5: Нет меню управления платежами (КРИТ)

### Текущее состояние ❌
- Хендлер `subscription.py` есть но спрятан
- Нет быстрого доступа из админ-панели
- Нет редактирования цен ботом

### Требуемое решение ✅
**Новый раздел в `/admin`:**
```
/admin → 💰 Управление платежами
├── 💳 Методы оплаты
│   ├── 💎 TON: xxx...abc ✅
│   ├── 💵 USDT: xxx...abc ✅
│   └── Добавить новый метод
├── 💲 Цены и планы
│   ├── 🆓 Free: $0/мес
│   ├── ⭐ Starter: $9/мес → Редактировать
│   ├── 🚀 Pro: $25/мес → Редактировать
│   └── 👑 Enterprise: $69/мес → Редактировать
├── 📊 История платежей
│   ├── Успешные: 127
│   ├── Ожидающие: 3
│   └── Просмотреть лог
└── 🎁 Выдать подписку (same as users menu)
```

---

## ПРОБЛЕМА 6: Безопасность — доступ сторонних юзеров к админке (КРИТ)

### Текущее состояние ❌
```python
# admin.py:507
@router.message(F.text)
async def admin_catch_all(message):
    if not is_platform_admin(message.from_user.id):
        return  # ← Просто return, но сообщение уже обработано
```

### Уязвимость
- Не-администраторы видят ввод как обработанный ✅
- Но если есть bug в `is_platform_admin()` — полная компрометация
- Нет логирования попыток несанкционированного доступа

### Требуемое решение ✅
1. **Strict RBAC в `is_platform_admin()`:**
   ```python
   ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "").split(",")))
   
   def is_platform_admin(user_id: int) -> bool:
       return user_id in ADMIN_IDS
   
   # В начало КАЖДОГО admin handler:
   if not is_platform_admin(callback.from_user.id):
       log.warning("Unauthorized admin access attempt: %d", callback.from_user.id)
       await callback.answer("⛔️ Только администратор", show_alert=True)
       return
   ```

2. **Логирование всех админ-действий:**
   ```sql
   CREATE TABLE admin_audit_log (
       id BIGSERIAL PRIMARY KEY,
       admin_id BIGINT NOT NULL,
       action TEXT NOT NULL,  -- "ban_user", "grant_plan", "edit_price"
       target_id BIGINT,
       details JSONB,
       created_at TIMESTAMPTZ DEFAULT now()
   );
   ```

3. **Проверка безопасности в старте:**
   ```python
   # main.py при старте
   admin_ids = os.getenv("ADMIN_IDS", "").split(",")
   if not admin_ids or not admin_ids[0].strip():
       log.critical("SECURITY: ADMIN_IDS not set! System is OPEN!")
       raise RuntimeError("ADMIN_IDS environment variable is required")
   ```

---

## ПРОБЛЕМА 7: "BotMother OS" — отдельное меню (АРХИТ)

### Текущее состояние ❌
```
/menu → BotMother OS (отдельное меню)
/start → Главное меню (другое меню?)
```

### Требуемое решение ✅
**ОДИН главный интерфейс:**
```
/start → Главное меню (это IS BotMother OS)
├── 🏗️ Infrastructure
│   ├── 📱 Аккаунты
│   ├── 🤖 Боты
│   ├── 📡 Каналы
│   ├── 👥 Группы
│   ├── 🔗 Кластеры
│   ├── 🌐 Прокси
│   └── ❤️ Здоровье
├── 👁️ Visibility
├── ⚙️ Operations
├── 📢 Broadcasts
├── 💬 Inbox/Relay
├── 🤖 AI Assistant
├── 🧠 Analytics
├── 💳 Billing
├── 👥 Referral
└── ⚙️ Settings

/admin → Администратор (отдельное только для админа!)
├── 👥 Пользователи
├── 💰 Платежи & Планы
├── 📊 Статистика
├── 🔔 Алерты
├── 🛡️ Безопасность
└── 📋 Аудит
```

---

## ПРИОРИТЕТ ИСПРАВЛЕНИЙ

| # | Проблема | Приоритет | Время |
|---|----------|-----------|--------|
| 1 | Управление пользователями + выдача подписок | 🔴 P0 | 4ч |
| 2 | AI Operations Manager (реальное выполнение) | 🔴 P0 | 6ч |
| 3 | Global Presence + anti-ban правила | 🔴 P0 | 3ч |
| 4 | Admin меню управления платежами | 🟠 P1 | 2ч |
| 5 | Безопасность & RBAC & логирование | 🔴 P0 | 2ч |
| 6 | "Message is not modified" в коде | 🟠 P1 | 1ч |
| 7 | Архитектура меню (один /start) | 🟡 P2 | 3ч |
| 8 | Расширение админ-возможностей | 🟡 P2 | 4ч |

**Итого для P0:** ~15 часов работы

---

## ПЛАН ДЕЙСТВИЙ (очерёдность)

### Этап 1 (2-3ч) — Безопасность первым делом
```
1. Усилить is_platform_admin() + логирование
2. Создать admin_audit_log таблицу
3. Проверка ADMIN_IDS при старте
```

### Этап 2 (4ч) — Управление пользователями
```
1. Таблица platform_users ← auto-fill при каждом /start от нового юзера
2. Handler admin_users.py с меню
3. Быстрые кнопки выдачи/отмены подписки
```

### Этап 3 (3ч) — Global Presence исправить
```
1. Добавить session_simulator в _exec_global_presence_channel
2. Проверка trust_score
3. Умные тайминги по времени суток
```

### Этап 4 (6ч) — AI Operations Manager
```
1. Новый handler ai_operations.py
2. FSM: описание задачи → парсинг Claude → operation_queue
3. Поддержка: create_channels, create_bots, create_groups, publish_posts
```

### Этап 5 (2ч) — Admin платежи меню
```
1. Новый раздел в admin.py
2. Редактирование цен (сохранение в config.py или БД)
3. История платежей
```

### Этап 6 (3ч) — Меню архитектура
```
1. Переделать на один /start = BotMother OS
2. Отдельный /admin для администраторов
```

---

## РЕЗУЛЬТАТ ПОСЛЕ ИСПРАВЛЕНИЙ

✅ Система **идеальна** для:
- Администратора: полный контроль, быстрое управление пользователями
- Клиентов: четкая навигация, реальное выполнение задач через AI
- Безопасности: логирование, RBAC, защита от несанкционированного доступа
- Anti-ban: соблюдение правил Telegram, умные тайминги

