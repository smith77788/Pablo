# Regression Registry

Before modifying any module, check which VERIFIED workflows could be affected.

If a module change touches code that a VERIFIED workflow depends on:
1. Re-test the affected workflow
2. If regression detected — add it here immediately
3. Fix regression before continuing other work

---

## Active Regressions

*(None — no VERIFIED workflows yet)*

---

## Regression Log

| Date | Module Changed | Workflow Affected | Severity | Status |
|------|---------------|-------------------|----------|--------|
| — | — | — | — | — |

---

## Regression Prevention Rules

- Pool call protection changes (try/except wrapping) must NOT change behavior for successful calls
- Any change to middleware (user_activity.py) must not break event logging
- Any change to activity_logger must not block bot responses
- Any change to op_worker must not break ongoing operations
- Any change to main.py startup sequence must not prevent bot launch
