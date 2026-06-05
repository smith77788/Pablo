# CLAUDE.md — BotMother Active Execution Rules

BotMother is an existing Telegram Infrastructure Operating System.

Current priority: make existing functionality actually work.

Do not optimize for feature count.
Optimize for working user workflows.

## Success

Progress means:
- source code changed
- defect fixed
- workflow restored
- integration repaired
- tests/checks run

Not progress:
- roadmap
- audit
- strategy
- analysis without implementation
- markdown-only changes

## Work loop

For every task:

1. Explore only the relevant files.
2. Identify the broken workflow or defect.
3. Find the root cause.
4. Fix the code.
5. Run available checks.
6. Verify the user workflow.
7. Fix nearby defects of the same class.
8. Report only what changed.

Do not stop after analysis.
Do not create new plans unless explicitly asked.

## Main priority order

1. STRIKE
2. Mass Operations
3. Operation Engine
4. Queue System
5. Worker System
6. Account Infrastructure
7. Proxy Infrastructure
8. Reports
9. Global Presence
10. Templates / DNA
11. Factories
12. Import Center
13. Audience / Parser
14. Posting / Mass Publishing
15. Search / Visibility
16. Dashboards / Analytics

## Definition of done

A feature is done only when the user can complete the intended workflow end to end.

For mass/risky operations, the workflow must include:
- validation
- preview
- confirmation
- queueing
- execution
- progress
- partial failure handling
- retry failed
- report
- history/audit

## Trace rule

For any broken feature trace:

User Action
→ Telegram UI
→ Handler
→ Service
→ Operation
→ Queue
→ Worker
→ Telegram/API/DB Action
→ Result
→ Report
→ User Feedback

Repair the broken link.

## No fake completion

Treat these as defects:
- TODO
- pass
- stubs
- fake success messages
- dead buttons
- handlers without execution
- services without real logic
- retry without retry
- progress without progress
- reports without data

Fix them when encountered.

## Architecture rule

Do not create duplicate execution paths.

Shared systems should be reused:
- Operation Engine
- Infrastructure Orchestrator
- Account Selection
- Proxy Selection
- Queue System
- Worker System
- Reports
- Audit/History

If a feature bypasses shared infrastructure, repair it.

## Checks

Run available:
- lint
- typecheck
- tests
- build
- migration checks

If a check cannot run, say why.

## Communication

Think and implement in English.
Respond to the user in Russian.

Final response format:

### Исправлено
### Что теперь работает
### Изменённые файлы
### Проверки
### Следующие дефекты
