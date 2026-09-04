# Network Builder

## Назначение

Модуль массового создания и управления сетями каналов, групп и ботов. Предоставляет шаблоны, топологию,.batch-создание и аналитику графов для построения связанных структур.

## Основные функции

- **Шаблоны сетей** — сохранение и повторное использование топологий
- **Экземпляры сетей** — создание реальных сетей из шаблонов
- **Управление узлами** — добавление каналов, групп, ботов в сеть
- **Связи между узлами** — определение отношений (admin, cross-post, и др.)
- **Визуализация графов** — получение данных для отображения топологии
- **Аналитика графов** — кластеризация, поиск кратчайших путей, обнаружение сообществ

## API эндпоинты

### Инициализация таблиц

```python
async def init_network_tables(pool: asyncpg.Pool) -> None:
    """Создание таблиц network builder если они не существуют."""
```

### Шаблоны

```python
async def create_template(pool: asyncpg.Pool, owner_id: int, name: str,
                          description: str = "", template_type: str = "channel_group",
                          nodes: list = None, edges: list = None) -> dict:
    """Создать шаблон сети. Возвращает {'ok': bool, 'id': int}."""

async def get_templates(pool: asyncpg.Pool, owner_id: int) -> list:
    """Получить все шаблоны сети."""

async def delete_template(pool: asyncpg.Pool, owner_id: int, template_id: int) -> dict:
    """Удалить шаблон сети."""
```

### Экземпляры сетей

```python
async def create_instance(pool: asyncpg.Pool, owner_id: int, template_id: int,
                          name: str) -> dict:
    """Создать экземпляр сети из шаблона. Возвращает {'ok': bool, 'id': int}."""

async def get_instances(pool: asyncpg.Pool, owner_id: int) -> list:
    """Получить все экземпляры сетей."""

async def get_instance_detail(pool: asyncpg.Pool, owner_id: int,
                              instance_id: int) -> Optional[dict]:
    """Получить детали экземпляра с узлами и рёбрами."""
```

### Управление узлами и рёбрами

```python
async def add_node(pool: asyncpg.Pool, instance_id: int, node_type: str,
                   label: str, ref_id: Optional[int] = None,
                   config: dict = None) -> dict:
    """Добавить узел в сеть. Возвращает {'ok': bool, 'id': int}."""

async def add_edge(pool: asyncpg.Pool, instance_id: int, source_node_id: int,
                   target_node_id: int, edge_type: str = "admin",
                   config: dict = None) -> dict:
    """Добавить связь между узлами. Возвращает {'ok': bool, 'id': int}."""

async def update_node_status(pool: asyncpg.Pool, node_id: int, status: str,
                             ref_id: Optional[int] = None) -> dict:
    """Обновить статус узла после создания."""
```

### Аналитика графов

```python
async def get_network_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Получить статистику сетей:
    - templates: количество шаблонов
    - instances: количество экземпляров
    - total_nodes: общее количество узлов
    - total_edges: общее количество связей
    """

async def get_graph_data(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Получить данные графа для визуализации по всем экземплярам."""

async def get_graph_clusters(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Кластеризация узлов по связности (алгоритм union-find)."""

async def find_shortest_path(pool: asyncpg.Pool, owner_id: int,
                             source_id: int, target_id: int) -> dict:
    """Найти кратчайший путь между двумя узлами (BFS)."""

async def get_community_detection(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Обнаружение сообществ (алгоритм label propagation)."""
```

## Примеры использования

```python
# Создание шаблона
template_nodes = [
    {"node_type": "channel", "label": "Основной канал"},
    {"node_type": "group", "label": "Чат для обсуждений"},
    {"node_type": "bot", "label": "Бот для уведомлений"},
]
template_edges = [
    {"source_idx": 0, "target_idx": 1, "edge_type": "cross_post"},
    {"source_idx": 0, "target_idx": 2, "edge_type": "admin"},
]
result = await create_template(pool, owner_id=123, name="Мой шаблон",
                               nodes=template_nodes, edges=template_edges)

# Создание экземпляра из шаблона
result = await create_instance(pool, owner_id=123, template_id=1, name="Рабочая сеть")

# Добавление узла
result = await add_node(pool, instance_id=1, node_type="channel", label="Дополнительный канал")

# Получение статистики
stats = await get_network_stats(pool, owner_id=123)
# {'templates': 5, 'instances': 12, 'total_nodes': 48, 'total_edges': 72}

# Кластеризация
clusters = await get_graph_clusters(pool, owner_id=123)
# {'ok': True, 'clusters': [...], 'node_cluster_map': {...}}

# Поиск пути
path = await find_shortest_path(pool, owner_id=123, source_id=1, target_id=5)
# {'ok': True, 'found': True, 'path': [...], 'length': 2}
```

## Типы узлов

- `channel` — Telegram канал
- `group` — Telegram группа
- `bot` — Telegram бот

## Типы связей

- `admin` — административная связь
- `cross_post` — кросс-постинг
- `ownership` — владение
- `partnership` — партнёрство