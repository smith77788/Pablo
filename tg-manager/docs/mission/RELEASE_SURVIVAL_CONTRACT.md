# RELEASE SURVIVAL CONTRACT

**Version:** 1.0 — Session 2026-06-06
**Priority:** ABOVE ALL OTHER DOCUMENTATION

This is the engineering contract Claude must honor in every session.
Violation of any rule here = session failure.

---

## 1. Main Goal

**No infrastructure destruction.**

Every session must leave the user's infrastructure safer than it was:
- Fewer crashing handlers
- Fewer silently failing operations
- Fewer unguarded DB calls
- Fewer fake success responses
- Fewer dead buttons

Priority is NOT adding features.
Priority IS making existing features survive production.

---

## 2. Credit Protection

- Maximum 20% of work time on research/reading
- Minimum 80% on actual code changes
- Never read a file without intent to edit it
- Never grep a module already analyzed this session
- Never create intermediate analysis documents — fix directly
- Prefer Edit over Write (smaller diffs)
- Batch independent tool calls in parallel

---

## 3. No-Loop Mode

- Never revisit COMPLETION_REGISTRY entries without new defect
- Never re-read a file read this session without a specific reason
- Never audit a WORKING/VERIFIED module
- Never re-analyze a problem with known root cause
- Never create roadmaps when the task is to fix a specific defect
- If you find yourself re-reading something → STOP, use what you know

---

## 4. Account Safety

Account destruction is irreversible. Highest risk priority.

**Cooldown rules:**
- After ANY Telegram-visible action, enforce cooldown per account
- Cooldown scales with action frequency and account trust_score
- Never send more than X messages/joins/invites per hour per account (use configured limits)

**Throttling:**
- Never bypass rate limits even if user requests speed
- Flood wait → mandatory sleep, retry with backoff, NOT skip
- 420 FloodWait → sleep exact seconds + jitter, log warning

**trust_score / health:**
- account.trust_score must be checked before selecting for risky ops
- health_score < threshold → exclude from active operations
- Banned/restricted accounts → never use for new ops until cleared

**Load / Risk:**
- Never assign >N concurrent operations to one account (N from config)
- High-risk ops (mass invite, dm campaign) → prefer high-trust accounts
- Load balance across account pool

**Rotation:**
- Rotate accounts across operations, never hammer single account
- Track last_used_at, prefer longest-idle accounts for sensitive ops

**Warmup:**
- Warmup sessions must not overlap with active operations on same account
- Warmup intensity: low → medium → high, never start at max

**Flood / Spam / Restriction signals:**
- On PEER_FLOOD → pause account, log, alert admin
- On SPAM_BOT restriction → suspend account, notify owner
- On account ban → mark banned, remove from all active queues

---

## 5. Proxy Safety

Proxy failure cascades to account failure. Critical risk.

**Health monitoring:**
- Proxies must have last_check_at and latency tracked
- Dead proxy → never assign to new connections
- High latency proxy → deprioritize, not block

**Quality:**
- Datacenter proxies → high risk for Telegram (use residential when possible)
- Rotation: never reuse same proxy for same account in short window

**Load:**
- Never assign too many simultaneous connections per proxy
- Track concurrent_connections, enforce max_connections limit

**Connectivity:**
- On connection failure → try next proxy in pool
- On repeated failure → mark proxy unhealthy
- Never retry failed op on same dead proxy

**Correlation:**
- Don't use same proxy for multiple accounts that must appear independent
- Proxy-to-account assignment must respect isolation groups

---

## 6. STRIKE — Complete Safety

STRIKE is the highest-risk operation. It touches real accounts in real time.

**Pre-flight checks (ALL must pass):**
- Account exists and is not banned/restricted
- Account has valid session
- Account trust_score ≥ minimum threshold
- Proxy is healthy and assigned
- Target is valid (channel/group exists, not private if not joined)
- Rate limits not exceeded for this account today

**During execution:**
- Every Telegram call wrapped in try/except
- FloodWait → sleep exact duration + jitter, then retry
- ChatWriteForbidden → log, skip target, continue
- UserBannedInChannel → skip, log
- Any unrecoverable error → stop strike, log reason, update status

**After execution:**
- Update last_action_at on account
- Apply cooldown
- Log result per target (success/skip/fail)
- Update op_log with final status and stats
- Never report success if any critical step failed silently

**Safety invariants:**
- STRIKE never continues after account ban signal
- STRIKE never sends without checking account health first
- STRIKE never operates on account with active warmup session

---

## 7. Mass Operations — Full Workflow

Every mass operation (bulk join, bulk leave, bulk invite, mass dm, mass publish) must:

1. **Validate inputs** — accounts exist, targets valid, limits not exceeded
2. **Show preview** — count of targets, estimated time, accounts to be used
3. **Require confirmation** — user must explicitly confirm
4. **Queue in op_log** — create record with status='queued'
5. **Execute via op_worker** — never execute inline in handler
6. **Track progress** — update op_log.progress periodically
7. **Handle partial failures** — log each failed item, continue on non-fatal
8. **Retry failed** — on FloodWait, retry; on permanent fail, skip and log
9. **Final report** — total/success/fail counts, duration, error summary
10. **History** — operation remains in audit log forever

Any mass op that bypasses op_worker and executes inline = critical defect.

---

## 8. Operation Engine

All ops go through a single execution pipeline:

```
Handler → validate → create_op(pool) → enqueue → op_worker picks up
op_worker → pre_flight → execute_step_by_step → update_progress
→ on_complete → report → notify
```

**Rules:**
- op_worker must never skip pre_flight checks
- progress updates: at minimum every N items or M seconds
- on crash: mark op as 'failed', log exception, notify admin
- on cancel: mark 'cancelled', release accounts back to pool
- never leave op in 'running' state after process restart

---

## 9. Queue / Worker

**Queue:**
- ops must be idempotent — safe to re-enqueue on restart
- never lose a queued op on process restart
- priority queue: high-priority ops execute first

**Worker:**
- on startup: recover all ops in 'running' state → reset to 'queued'
- max concurrent ops: respect config limit
- memory leak prevention: always cleanup op state after completion
- never block event loop: all DB and TG calls must be async

---

## 10. Warmup

Account warmup = progressive trust building. Destroying warmup = account loss.

**Rules:**
- Never start warmup on banned/restricted account
- Never run warmup and active ops simultaneously on same account
- Warmup steps: read content → react → comment → post → invite (sequential, not parallel)
- Each step: wait realistic human delay (minutes, not seconds)
- On any Telegram error during warmup: pause warmup session, log, do not retry immediately
- Warmup completion: update account.warmup_completed_at, increase trust_score
- Never mark warmup complete if not all steps passed

---

## 11. Private Channels

Private channels require special handling.

**Rules:**
- Never attempt to join private channel without valid invite link
- Invalid invite link → log error, do not retry, notify user
- After joining: verify membership before attempting any action
- Private channel invite operations: always use healthiest account
- Never use warmup accounts for private channel joins

---

## 12. Channel / Group / Bot Creation

Creation is irreversible. Must be done correctly.

**Channel creation:**
- Verify account can create (not banned, not at limit)
- Set all required fields (title, about, username if needed)
- After creation: store channel_id, access_hash immediately
- On creation failure: log, do not create partial records in DB

**Bot creation via BotFather:**
- Use dedicated BotFather session flow
- Verify bot was created (confirm bot_token received)
- Store bot_token encrypted immediately
- Never log bot_token in plain text

---

## 13. Factories — Complete Objects

Factories must create complete, functional objects — not stubs.

**Account factory:**
- Created account must have: valid session, proxy assigned, trust_score initialized, warmup_completed=False
- Never return partially initialized account

**Bot factory:**
- Created bot must have: bot_token stored, BotFather handshake completed, profile set
- Never return bot without valid token

**Channel factory:**
- Created channel must have: channel_id, access_hash, admin account linked
- Never return channel without access_hash

**Rules:**
- Factory failure → rollback all partial state
- Never leave orphaned records on partial failure
- Always verify created object is accessible before returning

---

## 14. Username / Naming Engine

Usernames are finite resources. Must not be wasted.

**Rules:**
- Never attempt to set username without first checking availability
- Username taken → try next from pool, do not retry same username
- Invalid username format → validate before attempting
- Username change cooldown: respect Telegram limits (14 days)
- Track assigned usernames in DB, never reuse active usernames
- Username generation: must produce valid TG usernames (5-32 chars, alphanumeric + underscore, no leading/trailing underscore)

---

## 15. Templates / DNA

Templates define operation behavior. Corruption = wrong behavior at scale.

**Rules:**
- Template changes require confirmation before applying to active ops
- Never overwrite template in use by running operation
- DNA profiles must be validated before activation
- Template syntax errors: catch at save time, not at execution time
- Template versioning: keep previous version, allow rollback

---

## 16. Global Presence

Global presence ops touch many accounts and channels simultaneously. High blast radius.

**Rules:**
- Always calculate total reach before executing
- Enforce per-account rate limits during global presence ops
- On account failure mid-op: continue with remaining accounts, log failure
- Progress tracking: mandatory, at minimum every 10 accounts
- Never execute global presence op without preview and confirmation

---

## 17. Import Center

Import operations bring external data into the system. Data integrity critical.

**Rules:**
- Validate all imported data before inserting into DB
- Duplicate detection: check before insert, not after
- Import failures: log each failed row, do not abort entire import
- Never import session strings without encryption check
- After import: verify record count matches expected

---

## 18. Audience / Parser

Parsing is a read-only scrape operation. Must not trigger Telegram anti-spam.

**Rules:**
- Parsing must use rate limits (delay between requests)
- Never parse more than N users/channels per minute per account
- On FloodWait during parse: pause, sleep, resume — never abort
- Parsed data must be deduplicated before storage
- Never use banned/restricted accounts for parsing

---

## 19. Posting / Mass Publishing

Posting is the highest-frequency visible operation.

**Rules:**
- Never post to channel without verifying account is admin
- Post scheduling: respect configured intervals, never post faster than limit
- Media validation: check file size and type before attempting send
- On post failure: log, skip channel, continue — never crash entire job
- Duplicate prevention: track posted message IDs, never repost
- Mass publish: always use op_worker, never inline execution

---

## 20. Reports — Real Data Only

Reports must show real data. Fake data = user makes wrong decisions.

**Rules:**
- Never return placeholder or hardcoded values in reports
- If data not available: return explicit "no data" state, not zero
- Report queries must have reasonable timeout
- Aggregate stats must be computed from actual op_log records
- Never cache stale reports beyond configured TTL
- On DB error in report: return error state, not empty chart

---

## 21. Bug Hunter Mode

When a defect is found:

1. Fix the immediate defect
2. Search for same class of defect in adjacent code
3. Fix all found instances before moving to next defect class
4. Never fix one instance and leave siblings broken

Same class examples:
- Unprotected pool call → search all handlers for more
- Missing try/except on Telegram call → search all op executors
- Fake success on mutation failure → search all similar mutations

---

## 22. Root Cause Rule

Never fix symptoms. Always fix root cause.

If pool call crashes → the root cause is missing try/except.
If operation hangs → the root cause is missing timeout.
If account gets banned → the root cause is missing rate limit enforcement.

Symptom fix = the handler shows an error message.
Root cause fix = the error cannot happen.

Prefer root cause fix. Add symptom handling only when root fix is impossible.

---

## 23. Partial Failure Rule

Mass operations will partially fail. This is normal. Handle it correctly.

**Requirements:**
- Track per-item result (success/fail/skip) separately
- On partial failure: continue remaining items
- Final status: 'partial' if any items failed but some succeeded
- Report: show success_count / total_count / fail_count
- Failed items: store reason for each failure
- Retry: allow retry of failed items only, not entire operation

Never return overall 'success' when any item failed silently.
Never return overall 'failed' when some items succeeded.

---

## 24. No Fake Success

Any of these is a critical defect:

- Handler returns "✅ Done" but nothing was saved to DB
- Operation shows "Running..." but never started
- Report shows "0 errors" but errors were silently swallowed
- Progress shows 100% before op completed
- `except: pass` on mutation that must succeed
- Status='ok' in activity_log when DB write failed

Fix immediately when encountered.

---

## 25. Session Success Definition

A session is successful if and only if:

1. At least one real code change was committed and pushed
2. At least one defect from OPEN_DEFECTS.md was closed
3. No new defects were introduced (no regressions)
4. All COMPLETION_REGISTRY entries remain closed
5. No production-critical file was left syntactically broken

A session is NOT successful if:
- Only documentation was written with no code changes
- Only analysis was performed with no fixes
- A previously fixed module was regressed
- Code was committed but not pushed
