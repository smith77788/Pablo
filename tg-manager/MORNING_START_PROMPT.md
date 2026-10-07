# Morning Start Prompt

Copy this into Claude Code / Codex at the start of a long work session.

```md
Start autonomous implementation mode for BotMother.

Read:
- CLAUDE.md
- AGENTS.md
- AUTONOMOUS_CLAUDE_PROMPT.md
- TASK_QUEUE.md
- CURRENT_STATE.md
- IMPLEMENTATION_LOG.md
- `.botmother/*`
- docs/*

Then work through TASK_QUEUE.md using the autonomous loop.

Do not stop after one task.
After each task:
- run relevant checks
- fix errors caused by your changes
- update IMPLEMENTATION_LOG.md
- update CURRENT_STATE.md
- update TASK_QUEUE.md
- choose the next safe task
- continue

Stop only for documented stop conditions.

Begin with repository inspection and TASK-001.
```
