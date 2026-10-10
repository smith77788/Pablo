# Auto-Registration Pro

## Назначение

Модуль автоматической регистрации аккаунтов Telegram через SMS-сервисы (5sim.net, sms-activate.org). Поддерживает одиночную регистрацию с обработкой 2FA и батч-регистрацию нескольких аккаунтов с автоматическим пропуском 2FA-номеров.

## Основные функции

- **Одиночная регистрация** — регистрация одного аккаунта с поддержкой 2FA
- **Батч-регистрация** — параллельная регистрация N аккаунтов (2FA пропускаются)
- **SMS-сервисы** — интеграция с 5sim.net и sms-activate.org
- **Расписание регистраций** — отложенная пакетная регистрация
- **Параметры устройства** — настройка производителя/версии для fingerprint
- **Прокси-ротация** — автоматический выбор прокси по стране номера
- **Статистика** — агрегированная статистика зарегистрированных аккаунтов
- **История** — журнал всех регистраций с пагинацией

## API эндпоинты

### Главное меню

```python
# bot/handlers/auto_registrar.py

@router.callback_query(AutoRegCb.filter(F.action == "menu"))
async def cb_autoreg_menu(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Главное меню авторега — показ сервиса, ключа, баланса."""
```

### Настройки SMS API

```python
@router.callback_query(AutoRegCb.filter(F.action == "settings"))
async def cb_autoreg_settings(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Настройки SMS API — выбор сервиса, ввод ключа."""

@router.callback_query(AutoRegCb.filter(F.action == "set_service"))
async def cb_autoreg_set_service(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    """Установка SMS-сервиса (5sim или sms-activate)."""

@router.callback_query(AutoRegCb.filter(F.action == "set_key"))
async def cb_autoreg_set_key(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Ввод API-ключа для выбранного сервиса."""

@router.message(AutoRegFSM.set_key)
async def msg_autoreg_set_key(msg: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Обработка введённого API-ключа."""
```

### Параметры устройства

```python
@router.callback_query(AutoRegCb.filter(F.action == "device_profile"))
async def cb_autoreg_device_profile(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Настройка параметров устройства (производитель/версия)."""

@router.callback_query(AutoRegCb.filter(F.action == "pick_manuf"))
async def cb_autoreg_pick_manuf(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Выбор производителя устройства."""

@router.callback_query(AutoRegCb.filter(F.action == "pick_appver"))
async def cb_autoreg_pick_appver(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Выбор версии приложения Telegram."""

@router.callback_query(AutoRegCb.filter(F.action == "reset_device_profile"))
async def cb_autoreg_reset_device_profile(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Сброс параметров устройства на автоматический выбор."""
```

### Одиночная регистрация

```python
@router.callback_query(AutoRegCb.filter(F.action == "pick_country"))
async def cb_autoreg_pick_country(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    """Выбор страны для регистрации."""

@router.callback_query(AutoRegCb.filter(F.action == "start"))
async def cb_autoreg_start(cb: CallbackQuery, callback_data: AutoRegCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Запуск регистрации (одиночной или батч)."""

async def _start_single_register(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    country: str,
    sms_client,
    state: FSMContext,
) -> None:
    """Одиночная регистрация: заказ номера → SMS → подтверждение."""
```

### Батч-регистрация

```python
@router.callback_query(AutoRegCb.filter(F.action == "batch_ask"))
async def cb_autoreg_batch_ask(cb: CallbackQuery, state: FSMContext) -> None:
    """Ввод количества аккаунтов для батча."""

@router.message(AutoRegFSM.batch_cnt)
async def msg_autoreg_batch_cnt(msg: Message, state: FSMContext) -> None:
    """Обработка введённого количества аккаунтов."""

async def _do_batch_register(
    pool: asyncpg.Pool,
    owner_id: int,
    country: str,
    cnt: int,
    sms_client,
    status_msg,
    progress_cb=None,
) -> dict:
    """Батч-регистрация cnt аккаунтов. 2FA пропускаются.
    
    Возвращает: {"ok": [str], "failed": [str]}
    """
```

### Пакетная регистрация с расписанием

```python
async def batch_register_with_scheduling(
    pool: asyncpg.Pool,
    owner_id: int,
    count: int,
    schedule: dict | None = None,
) -> dict:
    """Пакетная регистрация с опциональным расписанием.
    
    schedule = {
        "execute_at": datetime,      # когда запустить (None = немедленно)
        "interval_minutes": int,     # интервал повтора (0 = без повтора)
        "country": str               # код страны (по умолчанию 'russia')
    }
    
    Возвращает: {"task_id": ..., "status": "queued"|"started"|"completed", "count": N}
    """
```

### Статистика и история

```python
async def get_registration_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Агрегированная статистика регистрации.
    
    Возвращает:
        total: общее количество аккаунтов
        active: активные аккаунты
        inactive: неактивные аккаунты
        banned: забаненные аккаунты
        added_today: добавлены сегодня
        added_this_week: добавлены за неделю
        added_this_month: добавлены за месяц
        avg_trust_score: средний trust_score
        last_registration_at: время последней регистрации
    """

async def get_registration_history(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """История регистрации аккаунтов (новые первыми).
    
    Каждая запись: id, phone, first_name, username, acc_status,
    trust_score, added_at, proxy_id.
    """
```

## Примеры использования

```python
# Настройка SMS API
from bot.handlers.auto_registrar import router

# Выбор сервиса (автоматически через callback)
# Ввод API-ключа (автоматически через callback)

# Одиночная регистрация
from bot.handlers.auto_registrar import _start_single_register

# Батч-регистрация 5 аккаунтов в России
from bot.handlers.auto_registrar import _do_batch_register

result = await _do_batch_register(
    pool=pool,
    owner_id=123,
    country="russia",
    cnt=5,
    sms_client=sms_client,
    status_msg=status_msg,
)
print(f"Зарегистрировано: {len(result['ok'])}, ошибки: {len(result['failed'])}")

# Пакетная регистрация с расписанием
from bot.handlers.auto_registrar import batch_register_with_scheduling
from datetime import datetime, timedelta

result = await batch_register_with_scheduling(
    pool=pool,
    owner_id=123,
    count=10,
    schedule={
        "execute_at": datetime.now() + timedelta(hours=2),
        "country": "russia",
    }
)

# Статистика регистраций
from bot.handlers.auto_registrar import get_registration_stats

stats = await get_registration_stats(pool, owner_id=123)
print(f"Всего аккаунтов: {stats['total']}")
print(f"Активных: {stats['active']}")
print(f"Добавлено сегодня: {stats['added_today']}")

# История регистраций
from bot.handlers.auto_registrar import get_registration_history

history = await get_registration_history(pool, owner_id=123, limit=10)
for acc in history:
    print(f"{acc['phone']} — {acc['acc_status']} (trust: {acc['trust_score']})")
```

## FSM состояния

```python
class AutoRegFSM(StatesGroup):
    set_key = State()    # ввод API-ключа
    enter_2fa = State()  # ввод пароля 2FA после авторег
    batch_cnt = State()  # ввод количества аккаунтов для батча
```

## Конфигурация

- `_SMS_WAIT_SEC = 120` — максимальное ожидание SMS (2 минуты)
- `_INTER_REG_DELAY = 3.0` — пауза между регистрациями в батч-режиме
- `_SERVICES = {"5sim": "5sim.net", "smsactivate": "sms-activate.org"}` — поддерживаемые сервисы

## Поддерживаемые SMS-сервисы

| Сервис | Код | URL |
|--------|-----|-----|
| 5sim | `5sim` | 5sim.net |
| SMS Activate | `smsactivate` | sms-activate.org |

## Процесс регистрации

### Одиночная регистрация

1. Выбор страны
2. Заказ номера через SMS API
3. Запрос кода в Telegram (start_login)
4. Ожидание SMS (фоновый polling)
5. Подтверждение кода (confirm_code)
6. Если 2FA: FSM запрашивает пароль
7. Получение сессии (get_client_info_and_session)
8. Сохранение в tg_accounts (_save_account)

### Батч-регистрация

1. Ввод количества аккаунтов
2. Выбор страны
3. Для каждого аккаунта:
   - Заказ номера
   - Запрос кода (с прокси по стране номера)
   - Ожидание SMS
   - Подтверждение кода
   - Если 2FA: пропуск (считается как failed)
   - Сохранение сессии
4. Итоговый отчёт

## Хранение данных

### Таблица `tg_accounts`

```sql
CREATE TABLE tg_accounts (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    phone TEXT NOT NULL,
    session_str TEXT,
    tg_user_id BIGINT,
    first_name TEXT,
    username TEXT,
    device_model TEXT,
    system_version TEXT,
    app_version TEXT,
    lang_code TEXT,
    system_lang_code TEXT,
    proxy_id INTEGER,
    session_fp TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    trust_score NUMERIC DEFAULT 1.0,
    acc_status TEXT DEFAULT 'active',
    added_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, phone)
);
```

### Таблица `platform_settings`

```sql
-- Хранит настройки авторега:
-- sms_api_service: "5sim" или "smsactivate"
-- sms_api_5sim_key: API-ключ 5sim.net
-- sms_api_smsa_key: API-ключ sms-activate.org
-- autoreg_device_profile_{owner_id}: JSON с manufacturer/app_version
```

## Интеграция с другими модулями

- **Account Manager** — start_login, confirm_code, get_client_info_and_session
- **SMS API Engine** — get_sms_client для работы с SMS-сервисами
- **Proxy Pool Manager** — pick_registration_proxy для выбора прокси
- **Token Vault** — encrypt_token, session_fingerprint для безопасного хранения
- **Database** — set_platform_setting, get_platform_setting для настроек
- **Operation Queue** — для пакетной регистрации с расписанием
