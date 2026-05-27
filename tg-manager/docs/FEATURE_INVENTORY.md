# BotMother — Feature Inventory (v3.1, 2026-05-27)

## Implemented (✅ working)

### Infrastructure
- Multi-account management (QR/phone/session/import)
- Device fingerprints per account (20 Android profiles, schema_v23)
- Import existing channels/groups from Telegram into managed_channels
- Bot management (add, token, commands, webhooks, multigeo)
- Channel Factory (create, bulk-create, import, edit, stats, links)
- Group Factory (create, import, list, members, announcement)
- Cluster Manager (group channels/bots)
- Proxy Manager (socks5, check, bind)
- Health Dashboard (account state, trust scores)

### Operations
- Mass Ops (bulk edit bots, bulk join/leave)
- Operation Queue (queue, progress, cancel)
- Mass Publish (all channels / by account / dry-run + Smart Timing 30-90s)
- Network Broadcast (broadcast across bot network)
- Asset Templates + Apply Template (100%)
- Channel Operations (join/leave/publish/edit/contacts — full set)
- Smart timing in bulk create: typing_delay + 45-90s chaos + 5-10 min per 5

### Visibility
- Search Rankings (position tracking)
- Search Observations (confirmation pattern)
- Competitors (competitor monitoring)
- Visibility Reports [STARTER]
- Alerts (restriction_events aggregation) [FREE/STARTER]

### Behavioral layer
- Behavioral Events log
- Behavioral Engine (attention/habit/ecosystem/decay every 15 min)
- Search Memory (keyword affinity)
- Behavioral Dashboard [PRO]
- Session Simulator (integrated in channel_factory + channel_ops)

### Communication
- Relay (incoming dialogs, bot replies)
- Auto-reply (rules, triggers)
- CRM (tags, notes, user history)
- Funnels (auto-funnels with steps and delays)
- Schedules (scheduled broadcasts)
- Broadcast (broadcast with language segmentation)

### Monetization & settings
- Subscription (4 tiers + gates on 28+ features)
- Payment Checker (background check)
- Referral System (tier rewards, leaderboard)
- AI Assistant (Claude/Gemini API)
- Notifications Settings (per-user toggle) [FREE]

### Monitoring & security
- Account Monitor (restrictions)
- Trust Engine (scoring)
- Shadowban Monitor
- Operation Reports [STARTER]

### UX
- Descriptions for all BotMother OS menu sections
- Onboarding with 3 scenarios for new users
- Status icons ✅/⛔ in account list
- Descriptions in Channel/Group Factory, Mass Publish
- Descriptions in Visibility, Operations, Broadcasts, Inbox, Settings

## Partially implemented (⚠️)

- Operation Builder FSM — queue exists, wizard incomplete
- Post Template → Mass Publish auto-inject — redirect works, auto-prefill missing
- Behavioral collectors wiring — functions exist, not called from handlers

## Not implemented / stubs (❌)

- Operation Planner FSM — stub placeholder
- Notification Delivery — settings exist, actual bot.send_message not called
- `record_reentry` wiring in start.py
- `record_cross_nav` wiring in botmother_menu.py

## Broken or risky

- None currently known (all major flows working)

## Duplicate / chaotic areas

- `_progress_text` was previously duplicated in multiple files (fixed: use op_helpers)
- Some older handlers may still use `str = ""` in CallbackData (check on regression)

## Highest-leverage improvements

1. Operation Planner FSM → unlocks scheduled mass operations UI
2. Notification Delivery → makes the notifications settings actually useful
3. Post Template prefill → completes the template apply flow
4. Behavioral collectors wiring → activates the behavioral layer
5. Operation Builder wizard → completes the operations section
