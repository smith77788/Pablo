# CLAUDE.md — Правила проекта Infragram

## Язык
Всегда на русском. Не переходить на английский.

## Рабочий стандарт
Читай `docs/WORKING_STANDARD.md` — это основной документ, определяющий критерии принятия решений.

## Запреты
- Не удалять модули/хэндлеры/функциональность
- Не пересобирать с нуля
- Не разрушать архитектуру
- Исправлять до безопасного состояния, не удалять
- Не объявлять задачи завершёнными пока не проверены все сценарии

## Ветка
`claude/telegram-bot-services-xfAh6`

## Деплой
1. `python3 -c "import ast; ast.parse(open('file.py').read())"`
2. `git commit` → `git push`
3. Verify `/version` в боте

## Стек
Python 3.12 · aiogram 3.x · Telethon · asyncpg · PostgreSQL · Railway

## Модули
| Модуль | Файл | Назначение |
|--------|------|------------|
| Operation Engine | `services/op_worker.py` | Выполнение операций |
| Account Manager | `services/account_manager.py` | Управление аккаунтами |
| Strike Engine | `services/strike_engine.py` | Система жалоб |
| Ecosystem Brain | `services/ecosystem_brain.py` | Управление экосистемами |
| Mini App API | `services/mini_app_api.py` | API для Mini App |
| Bot Handlers | `bot/handlers/` | Обработчики Telegram бота |
