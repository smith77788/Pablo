# 27 — Task Queue Protocol

TASK_QUEUE.md is the active work queue for autonomous implementation.

The agent must treat it as the source of truth for what to do next, unless the user gives a more recent direct instruction.

## Task Statuses

Use these statuses:

- TODO
- IN_PROGRESS
- DONE
- BLOCKED
- SKIPPED
- NEEDS_USER_DECISION

## Task Format

Each task should use this structure:

```md
## TASK-001 — Short task name

Status: TODO
Priority: P0 / P1 / P2 / P3
Area: operation-engine / telegram-ux / database / safety / docs / import / visibility / billing / security
Risk: low / medium / high

Goal:
...

Acceptance Criteria:
- ...
- ...

Required Checks:
- lint
- typecheck
- tests

Notes:
...
```

## Priority Meaning

P0:
- broken existing behavior
- dangerous safety/security issue
- blocks other work

P1:
- foundational infrastructure
- Operation Engine
- mass-action capability
- Telegram UX reliability

P2:
- important product capability
- reporting
- templates
- import center
- visibility

P3:
- polish
- nice-to-have
- advanced features

## Selection Rules

Pick tasks in this order:

1. P0 safe fixes
2. P1 foundational systems
3. P1 UX/safety improvements
4. P2 product capabilities
5. P3 polish

Do not pick high-risk tasks if there are lower-risk foundational tasks available.

## Updating the Queue

After every cycle:

- mark completed tasks DONE
- mark blocked tasks BLOCKED with reason
- add discovered tasks when needed
- split large vague tasks into smaller safe tasks
- keep top of file focused on next work

## Anti-Chaos Rule

Never add hundreds of tasks.

Keep the active queue useful:
- 5–15 active tasks
- archive completed tasks lower in the file
- group tasks by priority and domain
