# Open Defects

Last updated: 2026-06-13

## OPEN (2 items)

- [HIGH] Private Channels — scan_owned_assets fails on ChannelPrivateError, kills entire scan silently
- [HIGH] Channel/Group/Bot Creation — BotFather dialog brittle; hard to fix without live interactive session

## FIXED (2026-06-13 session)
- reg_checker: "requires account" shown for channels → Bot API fallback, instant result
- entity_analyzer: radar stats missing from return dict → added get_entity_radar_stats()
- accounts.py: double callback.answer() in cb_pools_bulk_assign
- follow_toggle: always returned to page 0 → page param preserved
- account_manager: ConnectionTcpFull → ConnectionTcpObfuscated (protocol obfuscation)
- IP masking: CF relay (tg-relay.agentsmith77778888.workers.dev) via CF_RELAY_URL

## FIXED (2026-06-06, session 3 — 66+ items)
- Pool calls: 66 unprotected pool calls wrapped across all handlers
- account_warmup: parallel 4→2, FloodWait all action types
- STRIKE: ban/PeerFlood detection, warmup overlap guard
- Mass ops: account double-use lock
- Factory username: check_username_available() before set
- activity_log: batch insert failure → WARNING + re-enqueue
