# Infragram

Telegram-native infrastructure management and mass action operating system.

## Purpose

Manage large Telegram infrastructure: accounts, channels, bots, operations — all from one place.

## Stack

- Python 3.12, aiogram 3.x, Telethon, asyncpg
- PostgreSQL, Railway deployment
- Mini App (single-page HTML/JS)

## Key Modules

- `services/op_worker.py` — operation execution engine
- `services/account_manager.py` — account management
- `services/strike_engine.py` — strike/complaint system
- `services/ecosystem_brain.py` — ecosystem intelligence
- `bot/handlers/` — Telegram bot handlers
- `mini_app/` — Mini App frontend
