# RELEASE SURVIVAL CONTRACT v1.0
**Priority: ABOVE ALL OTHER DOCS. Violation = session failure.**

## 1. Main Goal
No infrastructure destruction. Every session leaves infrastructure safer:
fewer crashing handlers · fewer silent failures · fewer unguarded DB calls · fewer fake success responses

## 2. Credit Protection
- Max 20% time on research/reading, min 80% on code changes
- Never read a file without intent to edit it
- Never grep a module already analyzed this session
- Fix directly — no intermediate analysis docs, no plans unless asked
- Prefer Edit over Write · batch independent tool calls in parallel

## 3. No-Loop Mode
- Never revisit COMPLETION_REGISTRY entries without new defect signal
- Never re-read a file already read this session without specific reason
- Never audit a WORKING/VERIFIED module
- Never re-analyze a problem with known root cause
- Finding yourself re-reading something → STOP, use what you know

## 4. Account Safety (irreversible risk)
**Cooldowns:** After any Telegram-visible action, enforce cooldown per account. Scale with frequency and trust_score.
**Throttling:** Never bypass rate limits. FloodWait → sleep exact seconds + jitter. 420 → log warning.
**Selection:** trust_score checked before risky ops. health_score < threshold → exclude. Banned → never use.
**Load:** Never assign >N concurrent ops per account. High-risk ops → prefer high-trust accounts.
**Rotation:** Rotate accounts, track last_used_at, prefer longest-idle for sensitive ops.
**Warmup:** Never overlap warmup with active ops on same account. Steps: read→react→comment→post→invite.
**Flood signals:** PEER_FLOOD → pause+alert. SPAM_BOT restriction → suspend+notify. Ban → mark+remove from queues.

## 5. Proxy Safety
**Health:** Track last_check_at and latency. Dead proxy → never assign. High latency → deprioritize.
**Quality:** Datacenter proxies = high risk. Residential preferred. Never reuse same proxy+account in short window.
**Load:** Track concurrent_connections, enforce max_connections. On failure → try next. Repeated fail → mark unhealthy.
**Isolation:** Don't use same proxy for accounts that must appear independent.

## 6. STRIKE — Highest Risk
**Pre-flight (ALL must pass):** account not banned · valid session · trust_score ≥ min · proxy healthy · target valid · rate limits ok
**During:** Every TG call wrapped in try/except. FloodWait → sleep+jitter+retry. ChatWriteForbidden/UserBanned → skip+log.
**After:** Update last_action_at · apply cooldown · log per-target result · update op_log.
**Invariants:** STRIKE never continues after ban signal · never operates on account in warmup · never reports success if step failed silently.

## 7. Mass Operations — Required Workflow
validate inputs → preview (count/time/accounts) → user confirmation → queue in op_log → execute via op_worker (NEVER inline) → track progress → handle partial failures → retry failed → final report → permanent audit log

Any mass op that bypasses op_worker = critical defect.

## 8. Operation Engine
```
Handler → validate → create_op → enqueue → op_worker → pre_flight → execute → update_progress → report → notify
```
- Never skip pre_flight · progress updates every N items or M seconds
- On crash: mark 'failed', log, notify admin
- On cancel: mark 'cancelled', release accounts
- Never leave op 'running' after process restart

## 9. Queue / Worker
- Ops must be idempotent (safe to re-enqueue on restart)
- On startup: recover 'running' ops → reset to 'queued'
- Respect max concurrent ops · cleanup op state after completion · all calls async (never block event loop)

## 10. Warmup
- Never start on banned/restricted account · never overlap with active ops
- Steps sequential: read→react→comment→post→invite. Human delays (minutes, not seconds).
- On any TG error: pause session, log, do not retry immediately
- Never mark complete if any step failed. Completion → update warmup_completed_at + trust_score.

## 11–16. Feature Safety (brief)
**Private Channels:** Never join without valid invite link. Verify membership before action.
**Creation:** Verify account can create. Store channel_id/access_hash immediately. Never log bot_token plain.
**Factories:** Create complete objects only. On failure → rollback all partial state. No orphaned records.
**Username Engine:** Check availability before set. Taken → next from pool. Track in DB. Validate format (5-32 chars, alphanumeric+_).
**Templates/DNA:** Confirm before applying to active ops. Validate syntax at save time, not execution time.
**Global Presence:** Preview+confirmation mandatory. Per-account rate limits enforced. Track progress every 10 accounts.

## 17–19. Data Safety
**Import:** Validate all data before DB insert. Deduplicate. Log each failed row. Never abort entire import.
**Parser:** Rate limits between requests. FloodWait → pause+sleep+resume. Deduplicate before storage. Never use banned accounts.
**Publishing:** Verify admin rights. Respect intervals. Validate media. Track posted IDs. Always via op_worker.

## 20. Reports — Real Data Only
- Never return placeholder or hardcoded values
- "No data" explicit state (not zero) when data unavailable
- Never cache stale beyond TTL · on DB error return error state not empty chart

## 21. Bug Hunter Mode
1. Fix immediate defect
2. Search for same class in adjacent code
3. Fix all instances before moving to next class
Never fix one instance and leave siblings broken.

## 22. Root Cause Rule
Never fix symptoms. Always fix root cause.
Symptom fix = handler shows error. Root cause fix = error cannot happen.

## 23. Partial Failure
Track per-item result. Continue remaining on failure. Status 'partial' if mixed. Show success/fail/total counts.
Never return 'success' when any item failed silently. Never return 'failed' when some succeeded.

## 24. No Fake Success
Critical defects: `✅ Done` but nothing saved · `Running...` but never started · `0 errors` with swallowed exceptions · `except: pass` on required mutation · `status='ok'` when DB write failed.

## 25. Session Success
✅ Success = at least one real code change committed+pushed AND one defect closed AND no regressions
❌ Failure = docs-only · analysis-only · regression introduced · committed but not pushed
