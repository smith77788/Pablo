# Open Defects

Last updated: 2026-07-01

## OPEN (1 item)

- [HIGH] Channel/Group/Bot Creation — BotFather dialog brittle; hard to fix without live interactive session

## FIXED (2026-07-01 session #2 — Bot/Channel Factory unreachable from nav)
User report: "нельзя даже ничего сделать" in Bot Factory / Global Presence / bulk
creation. Root cause found by cross-referencing every CallbackData class's
"menu"-entry action against every file that constructs it project-wide:

- **BotFactCb (Bot Factory)** — the entire automated "create bot via @BotFather"
  wizard (account → count → name template → username template → confirm →
  submit) had ZERO entry points anywhere in the navigation. `BotFactCb(action=...)`
  was only ever referenced inside `bot_factory.py` itself (back-buttons pointing
  at its own screens) plus one unrelated `import_tokens` link from
  `ecosystems.py`. The feature was fully built and wired to `op_worker`, just
  completely unreachable — a dead module since some earlier refactor dropped its
  menu link. Fixed: added "🏭 Создать через BotFather" button to both the bots
  list (`bot/keyboards.py::bots_list`) and the empty-bots-list screen
  (`bot/handlers/bots.py::cb_list`).
- **ChanFactCb (Channel Factory)** — same pattern. Its rich menu (single/bulk
  create, import existing channels, bulk edit, stats, invite-link generation)
  was reachable only via one narrow sub-action (`seo_pick`) linked from
  Analytics; the actual `ChanFactCb(action="menu")` entry point was never
  linked from `botmother_menu.py`. Fixed: added "🏭 Фабрика каналов" button to
  `_assets_kb()`; also updated its internal "back" screen
  (`cb_chanf_back_ops`) to offer a route back to Assets, since it's no longer
  only reachable from Operations.
- Verified via full project-wide check (95 CallbackData classes, script
  cross-referencing every construction site against every file) that no other
  class is referenced from only one file — these were the only two orphaned
  modules.
- Global Presence (`GeoPresenceCb`) was re-verified end to end (menu → asset
  type → template → name/username pattern → geo → **account selection** →
  preview → confirm → launch → `op_worker._exec_global_presence_channel`) and
  found fully wired and reachable via "⚡ Операции" → "🌍 Гео-сеть: создать";
  it already has a full multi-select account-and-count step
  (`_show_accounts_step` / `cb_gp_acc_*`), contrary to the report. Not touched.

## FIXED (2026-07-01 session — product audit)
- content_safety._collapse: separator class only stripped `[\s._\-*]`, so obfuscation via
  `/ , | # +` etc between letters (e.g. "c/h/i/l/d p/o/r/n") bypassed the CSAM/terror filter
  entirely → widened to `[\W_]` (any non-word char), verified against the original bypass
  cases and the known-benign phrases ("cp file", "детские игрушки", "секс-шоп 18+", "против
  терроризма") to confirm no new false positives.
- payment_webhook._activate_subscription: confirmation message computed the shown expiry as
  a naive `now() + N months`, ignoring that the DB upsert extends from the *existing* expiry
  when a subscription is still active → a renewing user was told a shorter date than they
  actually got. Now reads the real `expires_at` back via `RETURNING` from the same upsert.
- 5 stale test-suite assertions updated to match intentional prior changes that had no
  matching test update (verified each against product code + git history before touching):
  admin.py subscription-gate callbacks use dedicated `F.data == "adm:gate*"` handlers rather
  than the generic action dispatcher (test_callback_integrity.py); bulk.py empty-state back
  button correctly targets `BmCb(action="operations")`, a live, sensibly-placed handler, not
  the removed `action="main"` (test_callback_integrity.py); accounts.py's `scan_owned_assets`
  call sites already fetch a fresh single-account row via `db.get_tg_account` right before
  scanning, just not via the literal `get_account_for_telethon` snippet the test expected
  (test_account_status_logic.py); `BOT_LIMITS["free"]` is deliberately 5 per commit
  `6f8cf81c` (revert of an earlier 1-bot demo cap), not 1 (test_subscription_features.py);
  proxy_manager's pro-plan gate is intact, just reformatted with a `back_callback=` kwarg
  (test_subscription_features.py).
- Verified (no defect found, ruled out after tracing the actual code): op_worker.py op_type
  dispatch table vs operation_bus.py OP_REGISTRY are fully consistent (no stuck-pending
  op_types); every `operation_bus.submit(...)` call site's op_type argument is a registered
  key (AST-verified across bot/handlers + services); a generic CallbackData
  filter-vs-construction mismatch scan across all ~60 CallbackData classes in the project
  turned up zero real dead buttons — all initial hits were static-analysis false positives
  (multi-line `F.action.in_({...})` filters, or handlers registered dynamically in a loop via
  `router.callback_query(ChanCb.filter(F.action == _prof_action))(...)`).

## FIXED (2026-06-14 session, continued)
- dm_engine progress: operation_queue.done_items never updated → run_campaign() accepts op_id, syncs per iteration
- _exec_check_accounts_health: exception → status "active" (fake success) → now "unknown"
- _exec_bulk_bot_edit: no bots → returned done 0/0 (fake success) → now returns failed
- net_broadcast.py: 4 pool calls without try (crash on DB error) → wrapped
- admin_users.py cb_grant_strike: 2 pool calls without try → wrapped
- accounts.py cb_scan_connect: pool.fetchval without try → wrapped
- channel_ops.py: 2 pool calls without try → wrapped

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
