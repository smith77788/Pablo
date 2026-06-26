# BotMother — Agent Rules (v3.1)

You are working on an existing project called BotMother.

Do not rebuild from scratch.
Do not code before repository inspection.

## Mandatory startup routine

Before coding, read in order:

1. `.botmother/00_READ_ME_FIRST.md`
2. `.botmother/01_CORE_CONTEXT.md`
3. `.botmother/02_EXECUTION_PROTOCOL.md`
4. `.botmother/03_FEATURE_CATALOG.md`
5. `.botmother/19_ARCHITECTURE_GOVERNANCE.md`
6. `.botmother/20_OPERATION_ENGINE_CONTRACT.md`
7. `.botmother/21_DATABASE_GOVERNANCE.md`
8. `.botmother/22_FEATURE_PRIORITY_SCORING.md`
9. `.botmother/23_TELEGRAM_UX_GOVERNANCE.md`
10. `.botmother/24_SELF_REVIEW_LOOP.md`
11. Then read `CLAUDE.md` for project-specific architecture, patterns, and current status.

## Core principles

BotMother is a Telegram-native infrastructure and mass-action operating system.

Core principle:
**Maximum Telegram capabilities. Minimum manual work.**

Mass operations are the product.
Everything important should eventually become an Operation.

Preserve existing architecture, flows, database conventions, and working logic.

## Source of truth

- Work in `tg-manager` for BotMother Telegram bot changes.
- Use the shared branch `claude/telegram-bot-services-xfAh6`.
- Do not create a separate bot project outside `tg-manager`.

## Runtime contract

- BotMother production runtime is Python 3.12.
- Keep BotMother code Python 3.12-compatible even if your local agent runs a newer Python.
- Do not use syntax newer than Python 3.12 in `tg-manager` (for example `except TypeError, ValueError:`).
- Use `except (TypeError, ValueError):` for multiple exception types.
- Do not inherit Python/runtime rules from neighboring projects or parent workspaces.
- Codex and Claude Code must both follow `AGENT_SYNC.md` before editing.

## Change discipline

- Inspect current files before editing.
- Preserve Claude Code changes and never overwrite unrelated dirty files.
- Stage only files that belong to the current task.
- Keep secrets out of git. `.env`, `.state`, logs, caches, and virtualenv files stay local.
- User-facing bot messages must be in Russian.

## Deploy discipline

After any BotMother code change intended for production:

1. Run syntax check: `python3 -c "import ast; ast.parse(open('file.py').read())"`
2. Commit only the intended files with a clear message.
3. Push to `claude/telegram-bot-services-xfAh6`.
4. Verify bot responds to `/version` after Railway auto-deploys.

## After work, report

- what was analyzed
- what changed
- files modified
- checks run
- risks
- remaining work
- next recommended step
