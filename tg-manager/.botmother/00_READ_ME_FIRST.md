# BotMother AI Memory — Read Me First

This folder is the persistent AI memory system for Claude Code / Codex working on the existing BotMother project.

The AI must NOT treat this as a one-time prompt.
The AI must use these files as the permanent product and engineering context.

## Mandatory startup routine

At the beginning of every new coding session:

1. Read this file first.
2. Read `01_CORE_CONTEXT.md`.
3. Read `02_EXECUTION_PROTOCOL.md`.
4. Read `03_FEATURE_CATALOG.md`.
5. Read the most relevant layer files for the requested task.
6. Inspect the repository before writing code.
7. Produce an audit/gap analysis before implementation.
8. Implement incrementally.

## Main rule

BotMother already exists.

Do not rebuild it.
Do not create a new app.
Do not replace working architecture unless there is a clear technical reason.

## Product essence

BotMother is a Telegram-native infrastructure and mass action operating system.

It must provide maximum Telegram capabilities with minimum manual work.

If a user can manually do an allowed action in Telegram, BotMother should eventually support doing it:
- once
- in bulk
- by filter
- by ecosystem
- by region
- by template
- with preview
- with retries
- with reports
- inside Telegram

## Do not let the product become

- a button dump
- a fake feature collection
- a generic dashboard
- a shallow bot builder
- a random set of modules
- a confusing technical panel

## Always preserve

- Telegram-native UX
- mass-action-first logic
- clear explanations
- previews before mass changes
- retry failed
- reports
- existing working code
