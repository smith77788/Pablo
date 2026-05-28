# Autonomous Claude Code / Codex Prompt for BotMother

Use this prompt when starting a long autonomous work session.

```md
You are working on the existing BotMother repository.

Read and follow:
- CLAUDE.md
- AGENTS.md
- `.botmother/*`
- docs/PROJECT_VISION.md
- docs/ARCHITECTURE.md
- docs/FEATURE_INVENTORY.md
- docs/ROADMAP.md
- TASK_QUEUE.md
- CURRENT_STATE.md
- IMPLEMENTATION_LOG.md

Your mode: AUTONOMOUS IMPLEMENTATION LOOP.

Do not rebuild the project from scratch.
Do not remove working systems.
Do not create fake features.
Do not create chaotic Telegram menus.
Do not stop after one task if safe tasks remain.

Work cycle:
1. Inspect repository state.
2. Read the task queue.
3. Choose the highest-priority safe task.
4. Implement the smallest complete useful slice.
5. Run relevant checks.
6. Fix errors caused by your changes.
7. Update docs and logs.
8. Mark task status in TASK_QUEUE.md.
9. Pick the next safe task.
10. Continue.

Stop only if:
- no safe tasks remain
- secrets/API keys are required
- a destructive decision is required
- user input is required
- tool/rate limits stop you
- continuing risks data loss

For every completed task, update IMPLEMENTATION_LOG.md with:
- task id
- goal
- changed files
- behavior before
- behavior after
- checks run
- results
- risks
- next recommended step

Always preserve BotMother's core principles:
- Telegram-native first
- everything important should scale
- everything becomes an Operation
- preview before risky actions
- confirmation before risky/mass actions
- progress tracking
- retry failed
- reporting
- clear UX
- no button dumps
- no fake placeholders

Start now by inspecting the repository and selecting the first safe task from TASK_QUEUE.md.
```
