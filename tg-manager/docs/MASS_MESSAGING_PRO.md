# Mass Messaging Pro

## Назначение

Модуль массовой отправки сообщений и управления рассылками в Telegram. Включает движок массового инвайтинга пользователей в группы/каналы и систему массовых рассылок по аудитории ботов с расписанием, A/B тестированием и шаблонами.

## Основные функции

- **Массовый инвайтинг** — добавление пользователей в группы/каналы по ID, @username или номерам телефонов
- **Массовые рассылки** — отправка сообщений всем подписчикам бота
- **Расписание рассылок** — отложенная отправка по времени или интервалу
- **A/B тестирование** — разделение аудитории на группы для тестирования вариантов
- **Шаблоны сообщений** — подстановка переменных {{USERNAME}}, {{FIRST_NAME}}, {{DATE}} и др.
- **Защита от флуда** — автоматическая обработка FloodWait, PeerFlood, rate limiting
- **Ротация аккаунтов** — переключение между аккаунтами при достижении лимитов
- **Content safety** — проверка контента на запрещённые материалы (CSAM/терроризм)

## API эндпоинты

### Массовый инвайтинг

```python
# services/mass_inviter_engine.py

async def invite_batch(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    user_refs: list[str | int],
) -> dict[str, Any]:
    """Добавить список пользователей (ID или @username) в группу.
    
    Возвращает:
        ok: количество успешных добавлений
        failed: количество неудачных попыток
        peer_flood: True если аккаунт перегрет
        errors: список ошибок
    """

async def invite_by_phones(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    phones: list[str],
) -> dict[str, Any]:
    """Добавить список номеров телефонов в группу.
    
    Алгоритм: ImportContactsRequest → InviteToChannel → DeleteContacts.
    Возвращает ту же структуру, что и invite_batch.
    """

def parse_user_refs(text: str) -> list[str]:
    """Парсинг строки с @username или ID через запятую/пробел/перенос."""

def parse_phones(text: str) -> list[str]:
    """Парсинг номеров телефонов: +79991234567 через любой разделитель."""

def parse_group_ref(text: str) -> str:
    """Нормализация ссылки на группу (@name / t.me/name / ID / invite link)."""
```

### Массовые рассылки

```python
# services/broadcaster.py

async def run(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession | None,
    broadcast_id: int,
    token: str,
    bot_id: int,
    text: str,
    photo_file_id: str | None = None,
    user_ids: list[int] | None = None,
    buttons: list[dict] | None = None,
    start_delay: float = 0.0,
    silent: bool = False,
) -> None:
    """Запуск рассылки с rate limiting и прогресс-трекингом."""

async def mass_broadcast_with_scheduling(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    text: str,
    schedule: dict,
) -> dict:
    """Создать рассылку с расписанием.
    
    schedule = {
        "schedule_minutes": int,        # отложенная отправка через N минут
        "scheduled_for": "ISO datetime", # точное время отправки
        "segment": "all" | "active_7d" | "active_30d",  # сегмент аудитории
        "buttons": [{"text": str, "url": str}]  # инлайн-кнопки
    }
    
    Возвращает: {"ok": True, "broadcast_id": int, "op_id": int, "total_users": int}
    """

async def ab_test_broadcast(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    variants: list[dict],
) -> dict:
    """A/B тестирование рассылок.
    
    variants = [{"text": str, "weight": int}, ...]
    Аудитория делится пропорционально weight.
    """

def cancel(broadcast_id: int) -> bool:
    """Отмена выполняющейся рассылки."""

def is_running(broadcast_id: int) -> bool:
    """Проверка, выполняется ли рассылка."""

async def resume_interrupted(pool: asyncpg.Pool) -> None:
    """Возобновление прерванных рассылок после перезапуска."""
```

### Управление через Mini App API

```python
# services/mini_app_api.py

async def mass_inviter_submit(request: web.Request) -> web.Response:
    """POST /api/miniapp/mass_invite — запуск массового инвайта."""
```

## Примеры использования

```python
# Массовый инвайт по username
from services.mass_inviter_engine import invite_batch, parse_user_refs, parse_group_ref

user_refs = parse_user_refs("@user1 @user2 123456789")
group = parse_group_ref("@my_group")
result = await invite_batch(session_string, acc, group, user_refs)
print(f"Добавлено: {result['ok']}, ошибки: {result['failed']}")

# Массовый инвайт по телефонам
from services.mass_inviter_engine import invite_by_phones, parse_phones

phones = parse_phones("+79991234567 +79997654321")
result = await invite_by_phones(session_string, acc, group, phones)

# Рассылка с расписанием
from services.broadcaster import mass_broadcast_with_scheduling

result = await mass_broadcast_with_scheduling(
    pool, owner_id=123, bot_id=456,
    text="Привет, {{FIRST_NAME}}! 🎉\n\nСегодня {{DATE}}",
    schedule={
        "schedule_minutes": 30,  # отправить через 30 минут
        "segment": "active_7d",  # только активные за 7 дней
        "buttons": [{"text": "Подробнее", "url": "https://example.com"}]
    }
)

# A/B тестирование
result = await ab_test_broadcast(
    pool, owner_id=123, bot_id=456,
    variants=[
        {"text": "Вариант A: Скидка 10%", "weight": 50},
        {"text": "Вариант B: Скидка 20%", "weight": 50},
    ]
)

# Запуск рассылки напрямую
from services.broadcaster import run

await run(pool, session, broadcast_id=789, token="BOT_TOKEN", bot_id=456,
          text="Сообщение для рассылки", silent=True)

# Проверка статуса
from services.broadcaster import is_running

if is_running(789):
    print("Рассылка ещё выполняется")
```

## Обработка ошибок

Движок автоматически обрабатывает следующие ошибки Telegram:

| Ошибка | Действие |
|--------|----------|
| `UserPrivacyRestrictedError` | Пропустить пользователя |
| `PeerFloodError` | Переключиться на другой аккаунт |
| `UserNotMutualContactError` | Пропустить (только для закрытых групп) |
| `FloodWaitError` | Пауза + запись в flood_engine |
| `UserAlreadyParticipantError` | Считать как успех |
| `ChatWriteForbiddenError` | Прервать операцию (нет прав) |

## Конфигурация

- `_BATCH_SIZE = 5` — размер пакета для инвайта (Telegram разрешает до 5 за раз)
- `_CONNECT_TIMEOUT = 15.0` — таймаут подключения к Telegram
- `_ACTION_TIMEOUT = 15.0` — таймаут выполнения действия
- `BROADCAST_DELAY` — задержка между сообщениями (из config.py)
- `_GROUP_DELAY = 3.0` — задержка для групп/каналов (20 msg/min)
- `_PROGRESS_FLUSH_INTERVAL = 50` — интервал обновления прогресса в БД

## Интеграция с другими модулями

- **Account Manager** — получение аккаунтов для инвайта/рассылки
- **Flood Engine** — учёт FloodWait при планировании
- **Strike Engine** — учёт жалоб при принятии решений
- **Content Safety** — проверка контента перед отправкой
- **Brand Injection** — добавление промо для free-tier ботов
- **Operation Queue** — очередь операций для выполнения
