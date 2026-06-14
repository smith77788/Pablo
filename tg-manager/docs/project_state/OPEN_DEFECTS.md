# Open Defects

Last updated: 2026-06-14

## OPEN (1 item)

- [HIGH] Channel/Group/Bot Creation — BotFather dialog brittle; hard to fix without live interactive session

## FIXED (2026-06-14 session)
- scan_owned_assets: ChannelPrivateError from iter_dialogs iterator kills entire scan, partial results lost
  → Fixed: manual __anext__ loop catches ChannelPrivateError per-dialog, scan continues
- cb_scan_all_resources (accounts.py): inline blocking loop ~8s/account → operation_bus.submit("scan_owned_resources")
- cb_chanf_import_all_accs (channel_factory.py): inline blocking import → operation_bus.submit("channel_import_all")
- cb_check_all_accounts (accounts.py): inline blocking health check → operation_bus.submit("check_accounts_health")
- cb_promote_all (channel_ops.py): inline blocking promote ~2s/account → operation_bus.submit("promote_all_admins")
- fsm_join_invite_combined bulk path (channel_ops.py): inline blocking → operation_bus.submit("bulk_join")
- fsm_botfather_username (channel_ops.py): asyncio.create_task bypass → operation_bus.submit("bot_factory")
- Dead _bg functions: 12 functions (1187 lines) removed from channel_ops.py and group_factory.py

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
