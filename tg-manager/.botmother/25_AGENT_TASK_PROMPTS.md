# Agent Task Prompts

Use these as repeatable prompts for Claude Code / Codex.

## First repository audit

Inspect the existing Infragram repository.

Do not code yet.

Find:
- stack
- architecture
- README / CLAUDE / AGENTS / docs
- bot framework
- database models
- workers / queues
- current features
- broken or incomplete flows
- duplicated logic
- reusable abstractions

Create or update:
- docs/ARCHITECTURE.md
- docs/FEATURE_INVENTORY.md
- docs/GAP_ANALYSIS.md
- docs/ROADMAP.md

Then propose the smallest safe high-impact implementation task.

## Operation Engine foundation

Implement the Operation Engine foundation while preserving existing architecture.

Requirements:
- OperationPlan
- OperationRun
- OperationResult
- target selection abstraction
- validation step
- preview step
- confirmation step
- execution step
- progress tracking
- failed item collection
- retry failed support
- report generation or report stub if report system does not exist yet
- history/audit integration where possible

Do not create fake features.
Do not bypass existing conventions.
Run available checks.
Update docs.

## Telegram UX cleanup

Improve Telegram-native UX without creating button dumps.

Requirements:
- clear menu titles
- short explanations
- grouped actions
- Back / Help navigation
- preview before risky actions
- confirmation before mass actions
- progress and final result messages
- retry/report options after failures

Preserve existing functionality.
