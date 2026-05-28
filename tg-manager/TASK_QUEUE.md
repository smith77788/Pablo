# TASK QUEUE

## P0 — Repository understanding
- [ ] Inspect repository structure
- [ ] Detect stack, bot framework, database, queues/workers
- [ ] Update docs/ARCHITECTURE.md
- [ ] Update docs/FEATURE_INVENTORY.md
- [ ] Update docs/GAP_ANALYSIS.md
- [ ] Update docs/ROADMAP.md

## P1 — Foundation
- [ ] Identify or implement OperationPlan / OperationRun / OperationResult
- [ ] Add preview/confirmation contract for mass operations
- [ ] Add result tracking and retry-failed abstraction
- [ ] Add basic report generation path
- [ ] Add safety pacing extension points

## P2 — Telegram UX
- [ ] Review menus for button dumps
- [ ] Add Back/Cancel/Help consistency
- [ ] Add clear explanations to mass-action flows
- [ ] Ensure risky actions require confirmation

## P3 — Targeting and templates
- [ ] Add reusable target selection abstraction
- [ ] Add template placeholder rendering
- [ ] Add template validation
- [ ] Add drift/template compare plan if existing structures support it

## P4 — Global Presence Factory
- [ ] Add Global Presence menu entry
- [ ] Implement guided flow: asset type → template → name → username → geo → accounts → preview → confirm
- [ ] Add geo seed/preset system or integrate existing geo data
- [ ] Add username uniqueness/fallback engine
- [ ] Add account pool selection/distribution
- [ ] Execute through Operation Engine
- [ ] Add progress, retry failed, report
- [ ] Document v1/v2 scope clearly

## P5 — Advanced
- [ ] Visibility tracking foundation
- [ ] Drift detection foundation
- [ ] Import center improvements
- [ ] Security/billing/referral hardening
