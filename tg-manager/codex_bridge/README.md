# Codex Telegram Bridge

This is the Telegram bridge used to talk to Codex while working on BotMother.
It lives inside `tg-manager` so Codex, Claude, Railway, and the Telegram bot
use one repository branch as the source of truth.

## Run Locally

1. Copy `.env.example` to `.env`.
2. Fill `TELEGRAM_CODEX_BOT_TOKEN`.
3. Add your Telegram ID to `TELEGRAM_ADMIN_USER_IDS`.
4. Start from the repository root:

```powershell
.\tg-manager\.venv\Scripts\python.exe .\tg-manager\codex_bridge\bot.py
```

The bridge works in `tg-manager` by default. Change `CODEX_PROJECT_DIR` only
when a task truly needs a different working directory.

## Behavior

- One Codex task runs at a time, so messages do not create duplicate workers.
- Long tasks send short progress updates every `TELEGRAM_PROGRESS_SECONDS`.
- All user-facing messages are in Russian.
- Text, code, Markdown, JSON, CSV, PDF, DOCX, and XLSX files can be attached.
- `.env` and `.state` are local only and must not be committed.
