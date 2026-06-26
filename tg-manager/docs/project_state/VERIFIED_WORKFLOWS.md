# Verified Workflows

**RULE: Do NOT add a workflow to VERIFIED without real end-to-end testing.**

If a workflow is VERIFIED, do NOT revisit it without:
- A new defect found
- A regression detected
- Requirements changed
- User explicitly requests review

---

## Verified

*(None yet — verification requires live testing)*

---

## Workflow Template

```
Workflow: [name]
Verified: [date]
By: [who/how]
Steps:
1. User action
2. Bot response
3. Operation launched
4. Progress visible
5. Completion reported
6. Data in DB
Edge cases tested:
- DB error during step X
- Flood wait during step Y
- Cancellation at step Z
```

---

## Pending Verification

- STRIKE single-account mini-strike
- Mass Publish via op_worker
- Bulk Join via op_worker
- Bulk Leave via op_worker
- Account Warmup full session
- DM Campaign launch → progress → completion
- Quick Post single channel
- Channel creation (single account)
- Bot creation via BotFather
