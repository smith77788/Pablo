# Module Status Registry

Statuses: PLANNED | IN_PROGRESS | PARTIAL | WORKING | VERIFIED | REGRESSION

Last updated: 2026-06-06

---

## Core Infrastructure

| Module | Status | Notes |
|--------|--------|-------|
| activity_log middleware | WORKING | warning/error split done; needs schema_v84 in prod |
| mark_handled_error | WORKING | 11 handlers covered |
| Pool call protection | IN_PROGRESS | channel_ops, account_warmup, mass_ops, dm_campaigns done |
| op_worker (main loop) | PARTIAL | executes ops, but some exec functions need verification |
| operation_bus | PARTIAL | submit works, result reporting needs check |
| activity_logger (queue) | WORKING | batch inserts, fire-and-forget |

## Operations

| Module | Status | Notes |
|--------|--------|-------|
| STRIKE | PARTIAL | access control works; staggered_strike needs safety hardening |
| Mass Publish | PARTIAL | inline bg task works; op_worker path needs verification |
| Bulk Join | PARTIAL | via op_worker _exec_bulk_join |
| Bulk Leave | PARTIAL | via op_worker _exec_bulk_leave |
| Bulk Bot Edit | PARTIAL | via op_worker _exec_bulk_bot_edit |
| Global Presence (channel) | PARTIAL | via op_worker |
| Global Presence (bot) | PARTIAL | via op_worker |
| Bulk Create Channels | PARTIAL | via op_worker |
| DM Campaigns | PARTIAL | launch works; dm_engine.run_campaign needs check |
| Quick Post | PARTIAL | wizard complete; channel posting needs check |
| BotFather Create | PARTIAL | automated dialog, stability unknown |

## Account Management

| Module | Status | Notes |
|--------|--------|-------|
| Account Warmup | IN_PROGRESS | pool calls protected; warmup logic correctness unknown |
| Account Health | PLANNED | trust_score updates, deactivation on fatal |
| Account Selection | PARTIAL | resource_selector exists |
| Proxy Management | PARTIAL | add/delete/detect works; failover unknown |
| Session Safety | PARTIAL | deactivates on AUTH_KEY error |

## UI / Handlers

| Module | Status | Notes |
|--------|--------|-------|
| botmother_menu | IN_PROGRESS | pool protection pending |
| admin.py | IN_PROGRESS | 34 unprotected pool calls |
| channel_ops | WORKING | pool calls fully protected |
| dm_campaigns | WORKING | pool calls fully protected |
| mass_ops | WORKING | pool calls fully protected |
| account_warmup | WORKING | pool calls fully protected |
| strike | WORKING | pool calls protected |
| proxy_manager | WORKING | pool calls protected |
| quick_post | WORKING | pool calls protected |
| global_presence | WORKING | pool calls protected |
| mass_publish | WORKING | pool calls protected |

## Reports & Analytics

| Module | Status | Notes |
|--------|--------|-------|
| Activity Log UI | PARTIAL | query works; data sparse before fixes |
| Operation Reports | PARTIAL | op_reports handler works |
| Health Dashboard | PLANNED | health_dashboard.py has unprotected calls |
| Infra Analytics | PLANNED | infra_analytics.py has unprotected calls |

## Factories & Creation

| Module | Status | Notes |
|--------|--------|-------|
| Channel Factory | PLANNED | channel_factory.py — 12 unprotected calls |
| Group Factory | PLANNED | group_factory.py — 8 unprotected calls |
| Bot Factory | PLANNED | bot_factory.py — 2 unprotected calls |

## Other

| Module | Status | Notes |
|--------|--------|-------|
| Private Channels | PLANNED | reading/management correctness unknown |
| Username Naming | PLANNED | no dedicated engine found |
| CRM | PARTIAL | crm.py — 1 unprotected call |
| Funnels | PLANNED | funnels.py — 8 unprotected calls |
| Ecosystems | PLANNED | ecosystems.py — 18 unprotected calls |
| Presence Pack | PLANNED | presence_pack.py — 18 unprotected calls |
| SEO | PLANNED | seo.py — 23 unprotected calls |
| Subscription | PARTIAL | subscription.py — 6 unprotected calls |
