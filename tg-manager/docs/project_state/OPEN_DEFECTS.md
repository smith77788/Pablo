# Open Defects

Format: [SEVERITY] MODULE — Description

Severities: CRITICAL | HIGH | MEDIUM | LOW

Last updated: 2026-06-06 (session 3)

---

## CRITICAL

- [FIXED-2026-06-06] activity_log — batch insert failure now WARNING (not DEBUG) + re-enqueue on transient error; db.py startup verifies critical tables exist and logs ERROR if missing

## HIGH

- [FIXED-2026-06-06] account_warmup — parallel warmup lowered 4→2 (reduces coordinated-activity ban risk); FloodWait now handled for ALL action types (was only join_channel)
- [FIXED-2026-06-06] STRIKE — ban/PeerFlood detection + warmup overlap guard added
- [FIXED-2026-06-06] Mass Operations — account double-use in concurrent ops fixed (mark/release lock)
- [HIGH] Private Channels — channel reading may fail silently for private/restricted channels
- [HIGH] Channel/Group/Bot Creation — creation operations unstable; BotFather dialog automation brittle
- [FIXED-2026-06-06] Factories — channel_factory: check_username_available() called before set_channel_username(); username taken → clear user message
- [FIXED-2026-06-06] retry/progress — operation_audit now written from channel_ops.py direct ops (join/leave single+bulk); write_op_audit() public helper added to op_worker

## MEDIUM

- [FIXED-2026-06-06] username/name uniqueness — check_username_available() wired into channel creation flow (pre-checks before set)
- [FIXED-2026-06-06] proxy safety — proxy failure now recorded in infra_memory on TimeoutError/OSError; score degrades immediately so future ops rank dead-proxy accounts lower
- [FIXED-2026-06-06] reports — activity_log batch failure visible at WARNING; TG-операции log now populated from channel_ops operations via write_op_audit()
- [FIXED-2026-06-06] admin.py — all 66 pool calls wrapped
- [FIXED-2026-06-06] botmother_menu.py — all 29 pool calls wrapped
- [FIXED-2026-06-06] health_dashboard.py — 16 pool calls wrapped
- [FIXED-2026-06-06] infra_analytics.py — all pool calls wrapped
- [FIXED-2026-06-06] subscription.py — all 6 pool calls wrapped
- [FIXED-2026-06-06] seo.py — 23 pool calls wrapped
- [FIXED-2026-06-06] ecosystems.py — 18 pool calls wrapped
- [FIXED-2026-06-06] presence_pack.py — 18 pool calls wrapped

## LOW

- [FIXED-2026-06-06] funnels.py — 8 pool calls wrapped
- [FIXED-2026-06-06] channel_factory.py — 11 pool calls wrapped
- [FIXED-2026-06-06] group_factory.py — 8 pool calls wrapped
- [FIXED-2026-06-06] gift_transfer.py — 8 pool calls wrapped
- [FIXED-2026-06-06] account_cleaner.py — 7 pool calls wrapped
- [FIXED-2026-06-06] cluster_manager.py — 5 pool calls wrapped
- [FIXED-2026-06-06] crm.py — 1 pool call wrapped
- [FIXED-2026-06-06] net_broadcast.py — 2 pool calls wrapped
- [FIXED-2026-06-06] relay.py — 4 pool calls wrapped

---

## Remaining open (2 items)

- [HIGH] Private Channels — channel reading may fail silently for private/restricted channels; scan_owned_assets iterates all dialogs but ChannelPrivateError kills the entire scan
- [HIGH] Channel/Group/Bot Creation — BotFather dialog automation brittle; requires interactive Telegram session, hard to fix without live bot token

---

## Recently Fixed

- [FIXED] Non-critical TG errors logged as status=error (now warning)
- [FIXED] Handled errors invisible in activity_log (mark_handled_error + middleware)
- [FIXED] account_warmup.py — 33 unprotected pool calls
- [FIXED] channel_ops.py — all 45 unprotected pool calls
- [FIXED] mass_ops.py — all unprotected pool calls
- [FIXED] dm_campaigns.py — all unprotected pool calls
- [FIXED] strike.py, proxy_manager.py, quick_post.py pool calls
- [FIXED] global_presence.py, mass_publish.py pool calls
