# BotMother — Gap Analysis (v3.1, 2026-05-27)

## Product gaps

| Gap | Status | Location |
|-----|--------|----------|
| Operation Planner FSM | ❌ stub | botmother_menu.py → op_planner |
| Operation Builder FSM wizard | ⚠️ partial | mass_ops.py (queue exists, no wizard) |
| Notification Delivery | ❌ not implemented | account_monitor.py, ranking_checker.py |
| Post Template → Mass Publish auto-inject | ⚠️ redirect only | mass_publish.py |
| `record_reentry` not called | ❌ | start.py → behavioral_engine |
| `record_cross_nav` not called | ❌ | botmother_menu.py handlers |
| Search Memory drill-down | ❌ list only | behavioral dashboard |
| Experiment conversion tracking | ❌ not called | auto_responder.py |
| Referral integration in relay/funnel | ❌ | relay.py, funnel_runner.py |
| Admin bulk tools | ❌ | admin.py |

## UX gaps

- Operation Planner has no working UI (stub message)
- Notification settings exist but notifications are never sent
- Template apply for post type doesn't auto-inject text into Mass Publish

## Architecture gaps

- Behavioral collectors not wired to handlers (functions exist in behavioral_engine.py)
- No webhook for payments (polling instead — less reliable)
- No dry-run mode outside Mass Publish

## Safety gaps

- No approval workflow for critical bulk operations (e.g., bulk leave all channels)
- No rollback mechanism for failed bulk operations

## Scalability gaps

- Operation Queue works but Builder wizard incomplete (hard to queue complex ops)
- No multi-user workspace / RBAC support

## Reporting gaps

- CSV export missing from Visibility Reports
- No topology map (graph of bot/channel/account connections)
- No Unified Asset Registry with filters

## Security gaps

- Sessions encrypted in DB (good)
- Bot tokens encrypted in DB (good)
- Payment webhooks not implemented (polling is less secure for payment verification)

## Priority order for fixing

1. **Operation Planner FSM** — users can't schedule operations
2. **Notification Delivery** — notifications settings are dead without delivery
3. **Post Template prefill** — template apply flow incomplete
4. **Behavioral collectors** — behavioral layer data not being collected
5. **Operation Builder wizard** — advanced users blocked
6. **CSV export** — visibility reports missing export
7. **Experiment conversion** — A/B testing incomplete
