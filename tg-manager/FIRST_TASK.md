# FIRST TASK

Inspect the existing BotMother repository.

Do NOT immediately code.

## Tasks

1. Detect stack and architecture.
2. Find README / CLAUDE / AGENTS / docs.
3. Analyze database models.
4. Analyze Telegram bot structure (handlers, routers, callbacks, states).
5. Analyze workers, queues, scheduling systems.
6. Analyze current features and incomplete systems.
7. Detect duplicated or chaotic logic.
8. Detect reusable abstractions.
9. Produce or update:
   - `docs/ARCHITECTURE.md`
   - `docs/FEATURE_INVENTORY.md`
   - `docs/ROADMAP.md`
   - `docs/GAP_ANALYSIS.md`

## Rules

- preserve working code
- avoid destructive rewrites
- do not create fake implementations
- prefer incremental improvements
- focus on reusable operation infrastructure
- identify the smallest high-impact next step

After analysis, propose the safest and highest-leverage implementation task.

## Current known gaps (as of v3.1)

See `CLAUDE.md` section 13 (GAP-АНАЛИЗ) for the current status.

High priority:
- Operation Planner FSM (UI to schedule operations)
- Notification Delivery (send bot.send_message based on notification_settings)
- Post Template → Mass Publish auto-inject
- Behavioral collectors (record_reentry, record_cross_nav) wiring

Medium priority:
- Operation Builder FSM wizard
- Experiment conversion tracking
- Visibility Report CSV export
- Search Memory drill-down
