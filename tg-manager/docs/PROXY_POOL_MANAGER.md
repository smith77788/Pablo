# Proxy Pool Manager

## Назначение

Модуль управления прокси-серверами для аккаунтов Telegram. Включает автоматический скрейпинг бесплатных SOCKS5 прокси, их валидацию, кэширование, а также интерфейс управления персональными прокси через бота/Mini App.

## Основные функции

- **Автоматический скрейпинг** — сбор бесплатных SOCKS5 прокси из публичных списков
- **Валидация прокси** — проверка доступности через api.telegram.org
- **Кэширование** — in-memory кэш валидных прокси с fallback в БД
- **Управление персональными прокси** — добавление, проверка, удаление пользовательских прокси
- **Определение геолокации** — определение страны/города прокси через ip-api.com
- **Проверка уникальности IP** — обнаружение дубликатов прокси
- **Массовый импорт** — добавление нескольких прокси за раз
- **Статистика пула** — метрики использования и доступности прокси
- **Ротация прокси** — round-robin выбор прокси для аккаунтов

## API эндпоинты

### Автоматический скрейпинг

```python
# services/proxy_scraper.py

async def scrape_and_refresh(pool: asyncpg.Pool) -> dict:
    """Полный цикл: fetch → deduplicate → validate → store → update cache.
    
    Возвращает:
        fetched: количество уникальных прокси из источников
        validated: количество проверенных прокси
        valid: количество валидных прокси
        duration_s: длительность в секундах
    """

async def get_pool_proxy(pool: asyncpg.Pool) -> Optional[str]:
    """Получить случайный валидный прокси из платформенного пула.
    
    Возвращает proxy_url (socks5://host:port) или None если пул пуст.
    """

async def record_proxy_result(pool: asyncpg.Pool, proxy_url: str, success: bool) -> None:
    """Обновить статистику прокси после использования.
    
    success=True: увеличивает success_count, сбрасывает fail_count
    success=False: увеличивает fail_count, помечает как невалидный при fail_count >= 3
    """

async def get_pool_stats(pool: asyncpg.Pool) -> dict:
    """Статистика для UI/админки.
    
    Возвращает:
        valid: количество валидных прокси
        total: общее количество в БД
        avg_latency: средняя задержка (мс)
        last_check: время последней проверки
    """

async def run_scraper_loop(pool: asyncpg.Pool) -> None:
    """Фоновый цикл: обновление пула каждые 6 часов."""
```

### Управление персональными прокси

```python
# bot/handlers/proxy_manager.py

# Добавление прокси
@router.callback_query(ProxyCb.filter(F.action == "add"))
async def cb_proxy_add(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Меню добавления прокси."""

@router.message(AddProxyFSM.enter_url)
async def msg_add_proxy(msg: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Обработка введённого URL прокси."""

# Массовый импорт
@router.callback_query(ProxyCb.filter(F.action == "add_bulk"))
async def cb_proxy_add_bulk(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Массовый импорт прокси (по одному на строку)."""

# Проверка прокси
@router.callback_query(ProxyCb.filter(F.action == "check_all"))
async def cb_proxy_check_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Проверка всех прокси на доступность."""

# Определение гео
@router.callback_query(ProxyCb.filter(F.action == "detect_geo"))
async def cb_proxy_detect_geo(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Определение геолокации прокси через ip-api.com."""

# Проверка уникальности IP
@router.callback_query(ProxyCb.filter(F.action == "check_ip_unique"))
async def cb_proxy_check_ip_unique(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Проверка на дубликаты IP в списке прокси."""

# Бесплатный пул
@router.callback_query(ProxyCb.filter(F.action == "free_pool"))
async def cb_proxy_free_pool(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Просмотр статистики и ручное обновление бесплатного пула."""
```

### Выбор прокси для аккаунтов

```python
# services/account_manager.py

async def pick_registration_proxy(
    pool: asyncpg.Pool,
    owner_id: int,
    country_code: str,
) -> tuple[int | None, str | None] | None:
    """Выбор прокси для регистрации аккаунта.
    
    Подбирает прокси с учётом страны номера для единообразия гео.
    Возвращает (proxy_id, proxy_url) или None.
    """

def set_pool_proxy_cache(valid_urls: list[str]) -> None:
    """Обновить кэш платформенного пула в account_manager."""
```

## Примеры использования

```python
# Получение случайного прокси из пула
from services.proxy_scraper import get_pool_proxy

proxy_url = await get_pool_proxy(pool)
if proxy_url:
    print(f"Используем прокси: {proxy_url}")
else:
    print("Пул прокси пуст")

# Ручное обновление пула
from services.proxy_scraper import scrape_and_refresh

result = await scrape_and_refresh(pool)
print(f"Обновлено: {result['valid']}/{result['fetched']} прокси за {result['duration_s']}с")

# Запись результата использования
from services.proxy_scraper import record_proxy_result

await record_proxy_result(pool, "socks5://1.2.3.4:1080", success=True)

# Статистика пула
from services.proxy_scraper import get_pool_stats

stats = await get_pool_stats(pool)
print(f"Валидных: {stats['valid']}, средняя задержка: {stats['avg_latency']}мс")

# Проверка доступности прокси
from bot.handlers.proxy_manager import _check_proxy_alive

result = await _check_proxy_alive("socks5://1.2.3.4:1080")
if result['alive']:
    print(f"Прокси доступен, задержка: {result['latency_ms']}мс")

# Определение геолокации
from bot.handlers.proxy_manager import _detect_proxy_geo

geo = await _detect_proxy_geo("socks5://1.2.3.4:1080")
print(f"Страна: {geo.get('geo_country')}, Город: {geo.get('geo_city')}")

# Выбор прокси для регистрации
from services.account_manager import pick_registration_proxy

picked = await pick_registration_proxy(pool, owner_id=123, country_code="RU")
if picked:
    proxy_id, proxy_url = picked
    print(f"Выбран прокси: {proxy_url} (id={proxy_id})")
```

## Источники бесплатных прокси

Модуль скрейпит SOCKS5 прокси из следующих источников:

- `github.com/TheSpeedX/PROXY-List`
- `github.com/ShiftyTR/Proxy-List`
- `github.com/hookzof/socks5_list`
- `github.com/monosans/proxy-list`
- `api.proxyscrape.com`

## Конфигурация

- `_REFRESH_INTERVAL_H = 6` — интервал обновления пула (6 часов)
- `_VALIDATE_TIMEOUT = 10.0` — таймаут проверки прокси
- `_VALIDATE_CONCURRENCY = 40` — максимальное количество одновременных проверок
- `_MAX_VALIDATE_CANDIDATES = 800` — максимум прокси для проверки за цикл
- `_MAX_FAIL_COUNT = 3` — удаление прокси после 3 последовательных ошибок
- `_MIN_POOL_SIZE = 20` — предупреждение при размере пула ниже этого значения
- `_PROXY_RE` — регулярное выражение для валидации формата прокси

## Хранение данных

### Таблица `platform_proxy_pool`

```sql
CREATE TABLE platform_proxy_pool (
    proxy_url TEXT PRIMARY KEY,
    proxy_type TEXT DEFAULT 'socks5',
    is_valid BOOLEAN DEFAULT TRUE,
    latency_ms INTEGER,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_check TIMESTAMPTZ
);
```

### Таблица `user_proxies`

```sql
CREATE TABLE user_proxies (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    label TEXT,
    proxy_url TEXT NOT NULL,
    proxy_type TEXT DEFAULT 'socks5',
    is_active BOOLEAN DEFAULT TRUE,
    is_alive BOOLEAN,
    latency_avg_ms INTEGER,
    geo_country TEXT,
    geo_city TEXT,
    success_rate NUMERIC,
    last_check TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

## Интеграция с другими модулями

- **Account Manager** — выбор прокси для аккаунтов (round-robin)
- **Auto Registration Pro** — прокси для регистрации новых аккаунтов
- **Proxy Hygiene** — проверка здоровья прокси
- **Proxy Policy** — политики использования прокси
- **Proxy Selector** — выбор оптимального прокси для задачи
