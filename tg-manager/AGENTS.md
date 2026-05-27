# BotMother Agent Rules

This directory is the shared BotMother working project for Codex, Claude Code,
the Telegram bridge, and Railway.

## Source Of Truth

- Work in `tg-manager` for BotMother Telegram bot changes.
- Use the shared branch `claude/telegram-bot-services-xfAh6`.
- Keep `main` fast-forwarded to the same commit when a Railway deploy must run
  through the existing GitHub Actions workflow.
- Do not create a separate bot project outside `tg-manager`.
- The Codex Telegram bridge lives in `tg-manager/codex_bridge`.

## Change Discipline

- Inspect current files before editing.
- Preserve Claude Code changes and never overwrite unrelated dirty files.
- Stage only files that belong to the current task.
- Keep secrets out of git. `.env`, `.state`, logs, caches, and virtualenv files
  stay local.
- User-facing bot messages must be in Russian and simple enough for a child to
  understand.

## Deploy Discipline

After any BotMother code change intended for production:

1. Run the smallest relevant checks.
2. Commit only the intended files.
3. Push to `claude/telegram-bot-services-xfAh6`.
4. Fast-forward `main` to the same commit when Railway deploy is triggered from
   `main`.
5. Check the GitHub Actions/Railway result.
6. If deploy fails, inspect the failure and fix it before moving on.

Railway CLI is the preferred direct deploy path when `RAILWAY_TOKEN` and project
context are available. GitHub Actions is the fallback path when direct Railway
access is not available locally.
