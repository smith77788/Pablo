# blackbird — модуль OSINT-поиска аккаунтов

Поиск аккаунтов по **username** или **email** более чем на 600 сайтах (соцсети,
форумы, сервисы разработчиков и т. д.). Основа — открытый инструмент
[Blackbird](https://github.com/antoniaci/blackbird) (автор Lucas Antoniaci),
список сайтов — проект [WhatsMyName](https://github.com/WebBreacher/WhatsMyName).

Модуль **полностью отдельный**: на `tg-manager` и `assistant` не влияет, своих
фоновых процессов не запускает, работает только по вызову.

## Состав

| Путь | Что это |
|---|---|
| `upstream/` | Код Blackbird без правок (коммит `471ef97` от 03.11.2026). Лицензия GPL-3.0 — `upstream/LICENSE` |
| `pablo_blackbird/` | Наша обёртка: Python-функция `search()` и CLI с выводом в JSON |
| `requirements.txt` | Зависимости Blackbird (ставятся в свой venv, не в venv tg-manager) |
| `tests/` | Тесты обёртки без сети |

## Использование

В контейнере (собирается корневым `Dockerfile` в `/app/blackbird`):

```bash
cd /app/blackbird
.venv/bin/python -m pablo_blackbird --username johndoe
.venv/bin/python -m pablo_blackbird --email johndoe@example.com
.venv/bin/python -m pablo_blackbird -u johndoe --filter "cat=social" --timeout 15
```

Вывод — JSON: `query`, `kind`, `found_count`, `accounts` (сайт, ссылка,
категория, метаданные профиля, если сайт их отдаёт). Флаг `--all` добавляет и
сайты, где аккаунт не найден. NSFW-сайты по умолчанию исключены
(`--allow-nsfw` включает).

Из Python (в том же venv):

```python
from pablo_blackbird import search
result = search(username="johndoe", filter_expr="cat=social")
for acc in result.found:
    print(acc["name"], acc["url"])
```

Локально:

```bash
cd blackbird
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pablo_blackbird -u johndoe
```

## Как устроено

* Upstream пишет `data/`, `logs/`, `results/` рядом со своим кодом, а в
  контейнере код принадлежит root. Поэтому обёртка копирует `upstream/` в
  `BLACKBIRD_WORKDIR` (по умолчанию `/tmp/blackbird`) и запускает его оттуда
  отдельным процессом. Скачанный список сайтов переживает обновление кода.
* При каждом запуске Blackbird сверяет список сайтов WhatsMyName с GitHub и при
  изменениях скачивает новый (`--no-update` отключает). Нужен выход в интернет.
* Запуски идут по одному (файловая блокировка): так у upstream не перемешаются
  результаты.
* AI-режим Blackbird (`--ai`) обёрткой не поддерживается: он отправляет имена
  найденных сайтов на сторонний сервер `ai.blackbird.run`.
* `INSTAGRAM_SESSION_ID` в окружении включает сбор метаданных Instagram (как в
  upstream).

## Обновление upstream

```bash
git clone https://github.com/antoniaci/blackbird /tmp/bb
rsync -a --delete --exclude .git --exclude .github --exclude docs \
  --exclude Dockerfile --exclude .env --exclude .gitignore /tmp/bb/ blackbird/upstream/
cp /tmp/bb/docs/LICENSE blackbird/upstream/LICENSE
cp /tmp/bb/requirements.txt blackbird/requirements.txt
cd blackbird && python -m pytest tests -q
```

После обновления поправьте хеш коммита в таблице выше.

## Правовое

Blackbird распространяется под GPL-3.0, список WhatsMyName — под CC BY-SA 4.0.
Инструмент проверяет только публично доступные страницы. Использовать его
стоит в рамках закона о персональных данных той страны, где ведётся работа.
