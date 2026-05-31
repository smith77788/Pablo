# BotMother Agent Sync Contract

This file is the shared operating contract for Codex and Claude Code.
Read it before editing BotMother.

## Project Boundary

- BotMother lives in `Pablo/tg-manager`.
- Do not edit unrelated projects in this repository for BotMother work.
- Do not create a parallel bot, proxy, or replacement app.
- Preserve existing architecture and extend incrementally.

## Runtime

- Production runtime: Python 3.12.
- Docker images: `python:3.12-slim`.
- Local agent work must stay Python 3.12-compatible.
- Do not use syntax newer than Python 3.12 in BotMother.
- Multiple exception handlers must use `except (TypeError, ValueError):`.

## Stack

- Telegram framework: aiogram 3.13.1.
- Database: PostgreSQL via asyncpg.
- Telegram user accounts: Telethon.
- Deploy target: Railway.
- Primary entrypoint: `tg-manager/main.py`.
- Primary dependencies: `tg-manager/requirements.txt`.

## Git And Deploy

- Shared development branch: `claude/telegram-bot-services-xfAh6`.
- GitHub Actions deploys from `main`.
- Do not force-push over the other agent's commits.
- Before pushing, fetch/rebase or fast-forward so both agents build on the latest remote state.
- If pushing production changes, push the shared branch first; fast-forward `main` only when deployment is intended.

## Change Discipline

- Inspect relevant files before editing.
- Keep changes scoped to the requested BotMother task.
- Do not rewrite broad files just for formatting.
- Do not stage unrelated changes.
- Do not store secrets in git.
- User-facing BotMother messages should be Russian.

## Architecture Rules

- BotMother is Telegram-native first.
- Mass actions should move toward Operation-style flows: collect input, preview, confirm, enqueue, progress, report, retry.
- Handlers should not perform risky mass infrastructure changes inline when an operation path exists.
- Reuse existing services, callbacks, keyboards, database helpers, and operation queues.
- Avoid duplicate handlers, parallel service layers, and one-off schema tables.

## Required Checks

Minimum before commit:

```powershell
python -m compileall -q tg-manager agents tools main.py orchestrator.py
```

If Python 3.12 is unavailable locally, run an AST grammar check with `feature_version=(3, 12)` and say so in the summary.

For production deploys, also verify the bot with `/version` after Railway finishes.
