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
- [MEDIUM] admin.py — 34 unprotected pool calls; admin dashboard may crash on DB errors
- [MEDIUM] botmother_menu.py — 29 unprotected pool calls (fix in progress)
- [MEDIUM] health_dashboard.py — 21 unprotected pool calls
- [MEDIUM] infra_analytics.py — 11 unprotected pool calls
- [MEDIUM] subscription.py — 6 unprotected pool calls; subscription gates may crash
- [MEDIUM] seo.py — 23 unprotected calls
- [MEDIUM] ecosystems.py — 18 unprotected calls
- [MEDIUM] presence_pack.py — 18 unprotected calls

## LOW

- [LOW] funnels.py — 8 unprotected calls
- [LOW] channel_factory.py — 12 unprotected calls
- [LOW] group_factory.py — 8 unprotected calls
- [LOW] gift_transfer.py — 8 unprotected calls
- [LOW] account_cleaner.py — 7 unprotected calls
- [LOW] cluster_manager.py — 5 unprotected calls
- [LOW] crm.py — 1 unprotected call
- [LOW] net_broadcast.py — 2 unprotected calls
- [LOW] relay.py — 4 unprotected calls

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
