# Open Defects

Format: [SEVERITY] MODULE — Description

Severities: CRITICAL | HIGH | MEDIUM | LOW

Last updated: 2026-06-06

---

## CRITICAL

- [CRITICAL] activity_log — schema_v84 may not be applied in production; table may not exist, causing silent loss of all event logs

## HIGH

- [HIGH] account_warmup — warmup logic correctness unverified; schedules and pacing may cause account bans
- [FIXED-2026-06-06] STRIKE — ban/PeerFlood detection + warmup overlap guard added
- [FIXED-2026-06-06] Mass Operations — account double-use in concurrent ops fixed (mark/release lock)
- [HIGH] Private Channels — channel reading may fail silently for private/restricted channels
- [HIGH] Channel/Group/Bot Creation — creation operations unstable; BotFather dialog automation brittle
- [HIGH] Factories — created objects may be incomplete (missing username, no admin, no description)
- [HIGH] retry/progress — partial failure handling unverified; failed items may be silently lost

## MEDIUM

- [MEDIUM] username/name uniqueness — no dedicated engine; collisions possible across accounts
- [MEDIUM] proxy safety — failover behavior on proxy death unverified
- [MEDIUM] reports — activity_log shows "no records" for users due to ok-only logging (FIXED in middleware, pending deploy)
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
- [LOW] channel_factory.py — 12 unprotected calls
- [LOW] group_factory.py — 8 unprotected calls
- [LOW] gift_transfer.py — 8 unprotected calls
- [LOW] account_cleaner.py — 7 unprotected calls
- [FIXED-2026-06-06] cluster_manager.py — 5 pool calls wrapped
- [FIXED-2026-06-06] crm.py — 1 pool call wrapped
- [FIXED-2026-06-06] net_broadcast.py — 2 pool calls wrapped
- [FIXED-2026-06-06] relay.py — 4 pool calls wrapped

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
