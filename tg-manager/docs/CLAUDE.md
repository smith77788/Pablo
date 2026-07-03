# CLAUDE.md — Правила проекта Infragram

## Язык

Всегда общаться, информировать и работать только на русском языке.
Никогда не переходить на английский в ответах пользователю.

## Запреты

- Никогда не удалять модули, хэндлеры, операции или функциональность
- Если модуль работает неправильно или опасно — исправить его до безопасного состояния
- Удаление = потеря функциональности. Всегда только доработка и улучшение
- Не пересобирать проект с нуля
- Не создавать новый проект
- Не разрушать существующую архитектуру
- Не заменять рабочие флоу без причины

## Ветка

`claude/telegram-bot-services-xfAh6`

## Деплой

После любого изменения:
1. Syntax check: `python3 -c "import ast; ast.parse(open('file.py').read())"`
2. Commit с описанием
3. Push в ветку
4. Verify: `/version` в Telegram боте

## Архитектура

- Telegram-native интерфейс
- Mass operations как основной продукт
- Все действия через operation_queue
- Circuit Breaker для безопасности
- Adaptive Pacing для anti-detection

## Стек

- Python 3.12, aiogram 3.x, Telethon, asyncpg
- PostgreSQL, Railway deployment
- Mini App (single-page HTML/JS)

## Ключевые модули

| Модуль | Файл | Назначение |
|--------|------|------------|
| Operation Engine | `services/op_worker.py` | Выполнение операций |
| Account Manager | `services/account_manager.py` | Управление аккаунтами |
| Strike Engine | `services/strike_engine.py` | Система жалоб |
| Ecosystem Brain | `services/ecosystem_brain.py` | Управление экосистемами |
| Mini App API | `services/mini_app_api.py` | API для Mini App |
| Bot Handlers | `bot/handlers/` | Обработчики Telegram бота |
