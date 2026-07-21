# PRODUCT_DEPTH_PLAN — journeys & living punch-list

Living document: the punch-list grows as you audit; strike through what's done.
This holds **task/plan state** — keep it OUT of the memory ledger (which is for
reusable lessons).

## Diagnosis (why depth-first)
A product feels raw/MVP not from missing features but from **unfinished journeys**:
dead buttons, silent successes, empty screens that dead-end, duplicates. So we finish
by user journey, not by feature. Breadth freeze: no new modules until the live ones
work end-to-end.

## Method
1. Pick ONE user journey. Trace it end-to-end: UI → route → handler → store → back to
   the screen.
2. At each step run the bug-class sweep-list (see `AUDIT_LEDGER.md`).
3. Fix concrete findings, each with a regression test that fails without the fix.
4. Mark the journey verified only when it works on the **real path** (not just a unit
   test) and every step passes the sweep-list.
5. Record verification in this file; record the *lesson* in the ledger.

## User journeys (fill in for your product)
> Order them; do the most core one first.

1. **<<Core journey #1>>** — <<from first step to visible real result>>.
2. **<<Journey #2>>** — …
3. **<<Journey #3>>** — …
4. **<<Observability & settings>>** — the dashboard is an *honest mirror* of state;
   every visible setting actually does something (no dead toggles).

## Cross-cutting checks (apply on EVERY screen of every journey)
- **Empty states** explain the next step (not a scary blank or endless spinner).
- **Honest feedback** — after an action the real outcome is visible (success/why-it-
  failed), never "queued" then silence.
- **Setting completeness** — the module has what an operator actually needs (limits,
  schedule, filters, behavior), and every toggle has a real effect.
- **No dead ends** — every screen has a logical next step; errors lead to a fix.
- **Loading/error states** exist — no white screen.
- **Consistency** — same names/icons for the same thing; no duplicates.
- **Responsive layout** — no horizontal scroll, no collapsed controls.
- **Onboarding** — a new user knows the first step.

## Punch-list
> Format: `[ ]`/`[x]` priority — screen/file — problem.

- [ ] …
