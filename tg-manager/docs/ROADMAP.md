# BotMother — Roadmap (v3.1, 2026-05-27)

## 1. Immediate fixes (🔴 Critical)

- **Operation Planner FSM**: FSM wizard → choose operation → choose time → confirm → write to `operation_queue.scheduled_for`
  - `op_worker.run()` already checks `scheduled_for` — only UI missing
- **Notification Delivery**: In `account_monitor.py` and `ranking_checker.py`, check `notification_settings`, call `bot.send_message(user_id, ...)` on events
- **Post Template → Mass Publish auto-inject**: `state.update_data(tpl_prefill={...})` in `asset_templates.py`, check in `mass_publish.py` cb_mpub_start

## 2. Foundation improvements (🟡 Medium)

- **Behavioral collectors wiring**:
  - `record_reentry` in `start.py` when user returns after 7+ days
  - `record_cross_nav` in `botmother_menu.py` navigation handlers
- **Operation Builder wizard**: Full FSM — choose op type → choose targets → configure params → preview → confirm → queue

## 3. Operation Engine

- Dry-run mode for all mass operations (currently only Mass Publish)
- Approval workflow for destructive bulk operations (bulk leave, bulk delete)
- Retry failed items in more operations
- Operation Templates: save op config as reusable template

## 4. Targeting / search

- Search Memory drill-down: from behavioral dashboard → history for specific keyword
- Advanced target selection: by cluster, tag, health score, last activity

## 5. Telegram UX

- Progressive disclosure in complex menus
- Global action palette (search across all features)
- Contextual recommendations based on behavioral scores

## 6. Templates / DNA / drift

- Template drift detection: compare current channel/group config vs template
- Template sync: apply template changes to all instances

## 7. Import / discovery

- Auto-discovery: scan account dialogs → suggest assets to add
- Bulk import from CSV
- Asset deduplication detection

## 8. Visibility

- CSV export for Visibility Reports
- Advanced competitor tracking (subscribe to changes)
- Cross-keyword correlation analysis

## 9. Reports

- Operation history with analytics (success rate, avg time)
- Experiment conversion tracking completion
- Referral impact analysis
- Unified asset health report

## 10. Security / billing / governance

- Payment webhook (replace polling)
- RBAC / multi-user workspaces
- Admin bulk tools
- Audit log export
- Topology map (graph of infrastructure connections)
