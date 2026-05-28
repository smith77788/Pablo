# TASK QUEUE

## P0 — CRITICAL: BotMother OS consolidation
- [ ] **IMMEDIATELY**: Убрать прямые команды: `/ai`, `/accounts`, `/ops`, `/ranking`, `/referral`, `/subscription`
- [ ] Все функции ДОЛЖНЫ входиться только через BotMother OS меню
- [ ] Дока: ARCHITECTURE_ISSUES.md (убедиться что все понимают проблему)
- [ ] Заменить на redirect: `/ai` → "Откройте BotMother → 🤖 AI Assistant"

## P0 — Repository understanding (СДЕЛАНО)
- [x] Inspect repository structure
- [x] Detect stack, bot framework, database, queues/workers
- [x] Update docs/ARCHITECTURE.md
- [x] Update docs/FEATURE_INVENTORY.md
- [x] Update docs/GAP_ANALYSIS.md
- [x] Update docs/ROADMAP.md

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
