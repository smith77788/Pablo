# Implementation Log

## r19 (2026-06-06) — Pool Safety + Strike + Factories
- 66 unprotected pool calls wrapped across admin.py, botmother_menu.py, health_dashboard.py, infra_analytics.py, subscription.py, seo.py, ecosystems.py, presence_pack.py, funnels.py, channel_factory.py, group_factory.py, gift_transfer.py, account_cleaner.py, cluster_manager.py, crm.py, net_broadcast.py, relay.py
- account_warmup: parallel 4→2, FloodWait for all action types
- STRIKE: ban/PeerFlood detection + warmup overlap guard
- Mass ops: account double-use lock fixed
- channel_factory: check_username_available() wired before set_channel_username()
- write_op_audit() public helper added to op_worker

## r18 (2026-06-04) — Strike + Contact Invite
- Strike: pre-fetch history + ID-matching (Сообщений: 0 fix)
- Strike: InputPhoto() instead of private _get_input_photo()
- Strike: escort preset (25 texts, 6 languages)
- Strike: status icons ⚔️/🟡/🟢/🔴 by effectiveness
- Contact invite: asyncio.gather for join+contacts+invite (was sequential 40+ min)
- Contact invite: @username instead of numeric ID for channel

## r17 (2026-06-02) — Strike Modes + Session Import + AI
- Strike crash fix: isinstance check for bool return from _escalate_to_spambot
- Strike modes: Fast/Normal/Maximum UI with schema_v53
- Session file upload: .session SQLite import via stdlib sqlite3
- AI: exponential backoff 2.0s/0.5s + 120s timeout + Retry button
- Approval workflows: schema_v51 + approval_flow.py
- RBAC workspaces: schema_v52 + workspaces.py

## r13–r16 (2026-05-26–30) — Foundation
- Device fingerprints (Android), behavioral engine, session simulator
- Operation Engine, Queue System, Account Health V2, Flood Intelligence Engine
- Template system: validation, placeholders, drift detection
- Import Center, Audience Parser, Account Warming, Proxy Intelligence
- Strike V1, Global Presence Factory V1–V3
- A/B experiments, Visibility Reports CSV, Search Memory

## Current session (2026-06-13)
- reg_checker: Bot API fallback for channels (no account needed), instant date display
- entity_analyzer: radar stats in return dict, background enrichment
- accounts.py: pool bulk assignment fixed
- subscription.py: Free Mode toggle + platform_settings table
- schemas v95–v97: seen_entities, name_history, entity_follows
- account_manager: ConnectionTcpObfuscated replaces ConnectionTcpFull
- CF relay: services/cf_relay.py + infra/cf_relay_worker.js + CF_RELAY_URL config
- Worker deployed: https://tg-relay.agentsmith77778888.workers.dev
