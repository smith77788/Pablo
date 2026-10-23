# services/proxy_selector

Модуль **Proxy Selector** — унифицированный выбор и оценка прокси для
Infragram. Обеспечивает выбор прокси с учётом качества, проверку
здоровья, автоматическую ротацию и аудит изоляции IP.

В Infragram прокси всегда привязаны к аккаунту (a.proxy_id). Модуль
предоставляет:
- Получение скоров прокси из infra_memory
- Запись результатов работы прокси в infra_memory
- Ранжирование аккаунтов по качеству прокси
- Быструю проверку здоровья прокси из in-memory состояния
- Валидацию IP-изоляции для предотвращения datacenter-банов

## Основные функции

### extract_ip_from_proxy(proxy_url)
Извлечение IP-адреса из URL прокси. Поддерживает формат
socks5://user:pass@host:port. Расшифровывает зашифрованные URL через
token_vault.

### is_datacenter_ip(ip_str)
Проверка, является ли IP известным datacenter IP. Возвращает
(is_datacenter, provider) где provider — название провайдера (AWS,
DigitalOcean, Hetzner и т.д.).

### validate_ip_diversity(accounts, max_per_ip=3)
Валидация изоляции IP: проверяет что аккаунты не делят слишком много IP.
Возвращает {valid, warnings, ip_usage, datacenter_warnings}.

### audit_proxy_isolation(pool, owner_id, max_per_ip=1)
Аудит изоляции: активные аккаунты, делящие один IP прокси (риск бана), и
аккаунты без прокси. Строгая изоляция — каждый аккаунт на своём IP.

### failover_dead_proxies(pool, owner_id)
Переназначение аккаунтов с мёртвым прокси на здоровый резервный.
Реально пробит каждый прокси. Соблюдает IP-изоляцию: один резервный IP —
одному аккаунту.

### get_proxy_score(proxy_url, action_type="default")
Качество прокси по опыту infra_memory. 0.5 = нейтральный/новый.
> 0.7 — хороший прокси, < 0.3 — проблемный.

### record_proxy_result(proxy_url, action_type, success, latency_ms=0.0)
Запись результата работы прокси в infra_memory (non-blocking, in-memory).

### rank_accounts_by_proxy_quality(accounts, action_type="default")
Переранжирование списка аккаунтов с учётом качества прокси. Аккаунты без
прокси или с нейтральным score идут последними.

### get_healthy_proxies(pool, owner_id, action_type="default", min_score=0.3)
Список прокси с достаточным quality score. Каждый элемент: {proxy_url,
geo_country, score, is_active}.

### check_proxy_health(proxy_url, action_type="default")
Сводка состояния конкретного прокси. Если in-memory score нейтральный,
выполняет реальную проверку подключения. Возвращает: {proxy_url, score,
status: 'good'|'degraded'|'bad'|'unknown', latency_ms?}.

### probe_proxy(proxy_url, timeout=10.0)
Форсированная async-проверка прокси через подключение к api.telegram.org.
Полностью async (aiohttp + aiohttp_socks), не блокирует event loop.
Возвращает {ok, latency_ms?, error?}.

### get_proxy_pool_stats(pool, owner_id)
Статистика прокси-пула: общее число, активные, мёртвые, без назначения,
средний score, geo-распределение.

### auto_rotate_proxy(pool, owner_id, account_id)
Авто-ротация прокси: назначает аккаунту лучший доступный прокси из пула.
Сортирует по score, исключает уже занятые (для изоляции).

### get_proxy_health(pool, owner_id, proxy_id)
Здоровье конкретного прокси: score, assigned accounts, geo, is_active,
is_alive.

## API эндпоинты

Все эндпоинты доступны через Mini App API (`/api/miniapp/proxy/...`).

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| GET | `/proxy/health/{proxy_id}` | Здоровье прокси |
| GET | `/proxy/pool-stats` | Статистика прокси-пула |
| POST | `/proxy/check/{proxy_id}` | Проверка прокси |
| POST | `/proxy/rotate/{account_id}` | Ротация прокси для аккаунта |
| GET | `/proxy/healthy` | Список здоровых прокси |
| GET | `/proxy/audit-isolation` | Аудит IP-изоляции |
| POST | `/proxy/failover` | Failover мёртвых прокси |

## Примеры использования

```python
from services.proxy_selector import (
    extract_ip_from_proxy, is_datacenter_ip, validate_ip_diversity,
    audit_proxy_isolation, failover_dead_proxies, get_proxy_score,
    rank_accounts_by_proxy_quality, get_healthy_proxies,
    check_proxy_health, probe_proxy, get_proxy_pool_stats,
    auto_rotate_proxy, get_proxy_health,
)

# Извлечение IP из прокси
ip = extract_ip_from_proxy("socks5://user:pass@1.2.3.4:1080")

# Проверка на datacenter IP
is_dc, provider = is_datacenter_ip("52.1.2.3")  # (True, "AWS")

# Валидация изоляции
diversity = validate_ip_diversity(accounts, max_per_ip=1)
if not diversity["valid"]:
    print(f"Предупреждения: {diversity['warnings']}")

# Аудит изоляции для владельца
audit = await audit_proxy_isolation(pool, owner_id=1, max_per_ip=1)
print(f"Без прокси: {audit['accounts_without_proxy']}")
print(f"Общие IP: {audit['shared_ip_groups']}")

# Получение скоров прокси
score = get_proxy_score("socks5://user:pass@1.2.3.4:1080")

# Ранжирование аккаунтов по качеству прокси
ranked = await rank_accounts_by_proxy_quality(accounts, action_type="join")

# Список здоровых прокси
healthy = await get_healthy_proxies(pool, owner_id=1, min_score=0.6)
for p in healthy:
    print(f"{p['proxy_url']}: score={p['score']}")

# Проверка здоровья прокси
health = await check_proxy_health("socks5://user:pass@1.2.3.4:1080")
print(f"Статус: {health['status']}, score: {health['score']}")

# Форсированная проверка прокси
probe = await probe_proxy("socks5://user:pass@1.2.3.4:1080")
print(f"Живой: {probe['ok']}, latency: {probe.get('latency_ms')}ms")

# Статистика прокси-пула
stats = await get_proxy_pool_stats(pool, owner_id=1)
print(f"Всего: {stats['total']}, активных: {stats['active']}, мёртвых: {stats['dead']}")

# Авто-ротация прокси
result = await auto_rotate_proxy(pool, owner_id=1, account_id=123)
print(f"Ротация: {result['message']}")

# Здоровье конкретного прокси
health = await get_proxy_health(pool, owner_id=1, proxy_id=456)
print(f"Score: {health['score']}, статус: {health['status']}")
```

## Зависимости

- `asyncpg` — PostgreSQL-пул
- `aiohttp` + `aiohttp_socks` — async-проверка прокси
- `services.infra_memory` — in-memory скоры и метрики прокси
- `services.token_vault` — расшифровка зашифрованных URL прокси