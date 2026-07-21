# AUDIT_LEDGER — cross-session agent memory

## Why this file
The codebase is bigger than one pass. Without a record of what's already been
checked, each session either repeats prior work or trusts docs instead of verifying
code. This ledger is growing review-coverage memory between sessions.

## How to keep it (memory ≠ dumping ground)
The chronological log below grows, but the **reusable knowledge lives in the
SWEEP-LIST** — keep that compact; it is the memory index. When the log grows large
(say > ~800 lines), distill new lessons into the sweep-list and trim old entries (the
detail stays in `git log`). **Do not store here:** code, file paths, structure (read
from the repo), git history, or duplicates of `CLAUDE.md` / `PRODUCT_DEPTH_PLAN.md`.
Highest-value entries are **user corrections** and **bug classes** — not feature
recaps.

## Entry format (one entry = one lesson)
```
## <area/files> — <date> — <one-line lesson>
Checked: <what exactly you looked at>
Found: <the bug/risk, or "nothing substantive">
Fixed: <yes/no; commit; regression test>
```
Before declaring an area verified, look here: if it's already listed with a recent
date and no open findings, don't re-check from scratch. Found a bug → fix +
regression test (same commit) + an entry here.

---

## SWEEP-LIST: bug classes (check each on every screen/handler you touch)

> Seed list — extend it with your own classes as you find them (one line each).

1. **Scope mismatch (owner vs cross-tenant/admin).** Count/list/filter scoped one way
   while the screen shows the other → "0 while screen shows N" or "0 rows → op
   silently fails." Counter scope must match the screen it links to.
2. **Second source of truth.** UI writes store A, engine reads store B → control
   "saves" but does nothing. Wire to the store the engine reads.
3. **Param accepted but never reaches effect / preview ignores its own filter.**
4. **Fake / silent success.** "Done" without checking the real outcome; counter =
   input size, not what happened. Failure reason must reach the screen.
5. **Dead control** — `onclick`/binding or API route that doesn't exist. Guard test.
6. **Duplicate definition** shadows the working one. Guard test (syntax won't catch).
7. **Irreversible mass action without guard** — no confirm/canary/per-target log/
   honest counter/rollback.
8. **Counter overflow on retry** — op re-runs same id (retry/watchdog/restart)
   without resetting progress → `done > total`. Reset on requeue AND at executor
   start; clamp the display.
9. **Paused without resume** — paused thing must have resume in the same place; verify
   engine idempotency before exposing resume.
10. **Timezone drift** — local → UTC(ISO) → tz-aware parse → back to local.

**Verification rules:** "verified" = trace to the *effect* (runner scheduled? param
reaches DB?) + one edge case — not "the endpoint exists." Audit the whole front end
including separately-loaded scripts. Check the real path, not just a green unit test.

---

## Log (newest first or append at the end — one entry = one lesson)

<!-- Example:
## billing/checkout.py — 2026-01-15 — refund toggle wrote to the wrong table
Checked: refund settings save → refund worker gate
Found: UI saved settings_json.refunds, worker read refund_policy table (2nd source)
Fixed: yes; commit abc1234; test_refund_single_source
-->
