# 26 — Autonomous Implementation Loop

This file defines how Claude Code / Codex should work when the user wants a long autonomous implementation session.

The agent must not stop after completing one small task if there are still safe, well-defined tasks available.

## Core Loop

Repeat this loop:

1. Read the project context:
   - CLAUDE.md
   - AGENTS.md
   - `.botmother/*`
   - docs/ARCHITECTURE.md
   - docs/FEATURE_INVENTORY.md
   - docs/ROADMAP.md
   - TASK_QUEUE.md
   - CURRENT_STATE.md
   - IMPLEMENTATION_LOG.md

2. Inspect the repository before changing code.

3. Pick the highest-priority safe task from TASK_QUEUE.md.

4. Verify the task does not conflict with:
   - existing architecture
   - product vision
   - safety rules
   - Telegram-native UX rules
   - Operation Engine principles
   - security requirements

5. Implement the smallest complete useful slice.

6. Run available checks:
   - lint
   - typecheck
   - tests
   - build
   - migration checks, if relevant

7. Fix failures caused by the change.

8. Update documentation:
   - CURRENT_STATE.md
   - IMPLEMENTATION_LOG.md
   - FEATURE_INVENTORY.md if feature status changed
   - ROADMAP.md if priorities changed
   - TASK_QUEUE.md by marking completed/blocked tasks

9. Commit or prepare a clear summary if commit is not allowed.

10. Select the next safe task and continue.

## Do Not Stop Just Because One Task Is Done

After each completed task, continue automatically unless a stop condition is reached.

## Stop Conditions

Stop only if:

- the user asks you to stop
- no safe tasks remain
- the next task requires secrets/API keys/user credentials
- the next task is destructive or irreversible
- the repository is in a confusing state that risks data loss
- tests fail for reasons unrelated to your change and you cannot safely isolate the issue
- implementation requires a product decision not present in the docs
- rate limits / tool limits prevent further work

## If Blocked

If blocked:

1. Mark the task as blocked in TASK_QUEUE.md.
2. Explain the blocker in IMPLEMENTATION_LOG.md.
3. Move to the next safe independent task.
4. Stop only if all remaining tasks are blocked or unsafe.

## Work Granularity

Prefer small, complete steps.

Good:
- add OperationPlan type and tests
- add preview renderer for one operation family
- add retry-failed data model
- add Telegram confirmation screen for existing operation

Bad:
- rewrite the whole app
- create 20 unfinished services
- create fake menu entries
- add placeholders pretending to work

## Required Behavior

Every implementation cycle must leave the project in a better, safer, more documented state.
