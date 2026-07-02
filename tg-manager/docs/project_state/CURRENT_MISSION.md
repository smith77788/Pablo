# Current Mission: Production Survival & Release Readiness

**Status:** ACTIVE  
**Updated:** 2026-06-06

## Mission

Make BotMother features actually work end-to-end for real users.

Not planning. Not auditing. Fixing.

## Priority Order

1. STRIKE — safety hardening, verified operation flow
2. Mass Operations — bulk join/leave/publish stable end-to-end
3. Operation Engine — op_worker, queue, retry, progress, reports
4. Queue System — status visibility, cancellation, retry
5. Worker System — account selection, proxy selection, rate limiting
6. Account Safety — session health, deactivation on fatal errors
7. Proxy Safety — validation, failover, geo-detection
8. Warmup — correct schedules, safe pacing, actual warmup logic
9. Private Channels — correct reading and management
10. Channel/Group/Bot Creation — stable, with proper error recovery
11. Factories — complete objects, not half-built
12. Username/Naming Engine — unique, validated, no collisions
13. Global Presence — correct scheduling, multi-account coordination
14. Reports — useful data, real numbers, not empty screens

## Definition of Done

A feature is DONE only when:
- User can complete the workflow end-to-end
- Errors are logged correctly (status=warning/error in activity_log)
- Progress is visible during operation
- Partial failures are handled (not silent)
- Retry works when it should
- Report shows real data after completion

## Active Work

- Pool call protection across all handlers (reduces crashes from DB errors)
- activity_log warning/error split (fixes invisible errors in admin logs)
- mark_handled_error infrastructure (connects handler errors to activity_log)

## Blocked

- DB migrations: schema_v84 must be applied to production for activity_log to work
