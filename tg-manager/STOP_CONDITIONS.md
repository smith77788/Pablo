# Stop Conditions

The autonomous agent should keep working through safe tasks, but must stop when continuing would be unsafe or impossible.

Stop if:

1. User explicitly says stop.
2. No safe tasks remain.
3. A task requires secrets, private credentials, payment keys, API keys, or Telegram sessions not present in the environment.
4. A task requires a product decision not documented anywhere.
5. A task may delete production data or cause irreversible destructive changes.
6. The repository state is corrupted or ambiguous enough that continuing risks data loss.
7. Tests fail for reasons unrelated to the current change and cannot be safely isolated.
8. Tool/rate limits prevent further work.
9. The next steps require external services that are unavailable.

If blocked on one task, do not stop immediately.
Mark it BLOCKED and move to the next safe independent task.
