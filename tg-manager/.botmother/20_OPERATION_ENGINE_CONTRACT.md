# Operation Engine Contract

Everything important should eventually become an Operation.

Telegram handlers should not directly perform dangerous or mass infrastructure changes.
Handlers should collect input, build an operation plan, show preview, request confirmation, enqueue execution, and display progress/reporting.

## Operation lifecycle

Every operation should support:

1. Intent collection
2. Target selection
3. Validation
4. Permission check
5. Preview
6. Risk / safety estimate
7. Confirmation
8. Execution
9. Progress updates
10. Partial failure handling
11. Retry failed
12. Cancellation where possible
13. Report generation
14. History entry
15. Audit logging

## OperationPlan

Recommended fields:

- id
- workspace_id
- user_id
- action_type
- target_type
- target_selector
- resolved_targets
- parameters
- template_id
- ecosystem_id
- safety_settings
- estimated_duration
- expected_changes
- risks
- validation_errors
- created_at

## OperationRun

Recommended fields:

- id
- plan_id
- status
- started_at
- finished_at
- total_targets
- succeeded_count
- failed_count
- skipped_count
- retryable_failed_count
- progress_percent
- current_stage
- report_id
- cancellation_requested

## OperationResult

Recommended fields:

- id
- run_id
- target_id
- target_type
- status
- message
- retryable
- error_code
- before_snapshot
- after_snapshot
- metadata
- created_at

## Required behavior

Mass operations must:

- show target count before execution
- show what will change
- show what may fail
- require confirmation
- store per-target results
- allow retrying failed items where safe
- generate a report
- update history

## Idempotency and retry safety

Operations should be:

- idempotent where possible
- resumable where possible
- retry-safe where possible
- observable
- auditable

Avoid duplicate execution when a user presses buttons twice.
