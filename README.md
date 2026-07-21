# Agent Operating System — memory + engineering discipline for coding agents

A file‑based **operating manual and cross‑session memory** for AI coding agents
(Claude Code, Cursor, Aider, or any agent that reads repo markdown). Zero
dependencies, no daemon, no vector DB — just markdown the agent reads on startup
and appends to as it learns.

It is **not** a "remember my preferences" note system. It encodes the *discipline*
that makes an agent (or a swarm of agents) ship correct, deep work on a real
codebase without re‑deriving context every session:

- a **bug‑class sweep‑list** the agent checks on every screen/handler it touches,
- a **depth‑first journey method** (finish user journeys end‑to‑end, don't sprinkle
  half‑features),
- a **"verified means traced to effect"** rule (not "the endpoint exists"),
- a **multi‑agent git protocol** (many agents, one branch, no clobbering),
- a **definition of done** with "the regression test must fail without the fix,"
- **honest reporting** (never present unverified work as verified),
- a **self‑maintaining memory ledger** with a compact, scannable index.

> Philosophy in one line: **most "AI memory" products give you recall; this gives
> you judgement.** Recall (who you are, what the project is) is the easy 20%. The
> hard 80% — not shipping dead buttons, silent successes, and half‑finished
> journeys — lives in the discipline files here.

---

## Why files (not a vector DB)

| Aspect | Markdown files (this) | Vector DB (mem0/Chroma/…) |
|---|---|---|
| Recall | Deterministic — the index loads every session | Similarity search — can miss or surface noise |
| Dependencies | Zero | A store and/or embeddings provider |
| Inspection | Open the file, diff in git | Query the store; edits are opaque |
| Review by humans | It's just markdown in your repo | Requires tooling |
| Multi‑agent sharing | Native — it's versioned in the repo | Shared service + write coordination |

Trade‑off: this doesn't scale to thousands of documents. It's built for one repo
and the handful of hard‑won rules that keep work on it correct. For large document
retrieval, use a vector store; for *how a team of agents should operate on your
code*, use files.

**Design choice — memory lives in the repo, not in `~/.claude`.** Because it's
committed to git, every agent (and every human) on the project shares the same
memory, it's reviewable in PRs, and it travels with the code. Global per‑user
memory is fine for personal preferences; project‑committed memory is what makes a
*swarm* coherent.

---

## The files

```
your-repo/
  CLAUDE.md                    # the skeleton: loaded every session, kept small
  docs/
    AGENT_PROTOCOL.md          # the full operating manual (read before working)
    AUDIT_LEDGER.md            # cross-session memory: a SWEEP-LIST index + a log
    PRODUCT_DEPTH_PLAN.md      # living plan: user journeys + punch-list
```

- **`CLAUDE.md`** — the index. Language rule, hard gates (one line each),
  load‑bearing project facts, pointers. **Keep it under ~150 lines** — it is read
  every session and a bloated index gets skimmed or silently truncated.
- **`docs/AGENT_PROTOCOL.md`** — the depth: work mode, multi‑agent rules, the bug
  classes in full, production‑safety for mass/irreversible actions, how to verify,
  the definition of done, and escalation.
- **`docs/AUDIT_LEDGER.md`** — memory between sessions. Opens with a **SWEEP‑LIST**
  (the reusable core: bug classes + verification rules); below it, a chronological
  log of "one entry = one lesson." The sweep‑list is what agents actually reuse; the
  log is prunable history.
- **`docs/PRODUCT_DEPTH_PLAN.md`** — the living plan: the map of user journeys, what
  is verified end‑to‑end, and the open punch‑list. This is *task state*, kept out of
  the memory ledger on purpose.

The split matters: **instructions** go in `CLAUDE.md`/`AGENT_PROTOCOL.md`,
**learned facts and bug classes** go in `AUDIT_LEDGER.md`, **task/plan state** goes
in `PRODUCT_DEPTH_PLAN.md`. Don't mix them — mixed files rot.

---

## Setup

```bash
git clone <this-repo>
cd <this-repo>

python3 setup.py --project /path/to/your/repo     # install into a repo
python3 setup.py --project /path/to/your/repo --force   # overwrite existing
```

Or just copy `templates/*` into your repo (`CLAUDE.md` at the root, the rest under
`docs/`). Then **edit the placeholders** — every template has `<<ANGLE‑BRACKET>>`
markers for the things only you know (your stack, your canonical branch, your
irreversible‑action layer, your language rule).

---

## The bug‑class sweep‑list (the heart of it)

This is the part worth stealing even if you use nothing else. On every screen,
handler, or executor an agent touches, it checks these — because each one shipped a
real, user‑visible defect somewhere:

1. **Scope mismatch** — a count/list/filter scoped to the owner where the screen
   shows a cross‑tenant/admin view (or vice‑versa) → "0 while the screen shows N,"
   or "0 rows → the operation silently fails."
2. **Second source of truth** — the UI writes to store A, the engine reads store B →
   the toggle "saves" but changes nothing (a silent success).
3. **Param accepted but never reaches effect** — validated in the handler, dropped
   before the DB/side‑effect; or a preview number ignores the filter it previews.
4. **Fake / silent success** — a "done" toast without checking the real outcome; a
   counter that reports the input size, not what actually happened.
5. **Dead control** — a button whose click handler / API route doesn't exist. (Ship
   a test that greps the UI for handlers/routes and asserts each is defined.)
6. **Duplicate definition** — a second handler/route/function shadows the working
   one. (Ship a test that fails on duplicates — syntax checks won't catch it.)
7. **Irreversible mass action without a guard** — blasting to *all* targets in one
   tap, no confirm, no canary, no per‑target log, no honest counter.
8. **Counter overflow on retry** — an operation re‑runs under the same id (retry /
   watchdog / restart) without resetting progress → `done > total`.
9. **Paused without resume** — anything that can be paused must have a resume control
   *in the same place*; and verify the engine is idempotent before exposing resume
   (does it re‑send?).
10. **Timezone drift** — local input → store as UTC (ISO) → parse tz‑aware → display
    back in local. Break any hop and the time is wrong.

**Verification rules:** "verified" = trace the chain *to the effect* (is the runner
actually scheduled? does the param reach the DB?) plus one edge case — not "the
endpoint exists." Audit the whole front end, including any separately‑loaded script
files. Check the real path, not just a green unit test.

---

## Keeping memory healthy

- The **index is sacred**: keep `CLAUDE.md` and the ledger's sweep‑list small and
  scannable. A memory file no one can skim is a memory file no one reads.
- **Distill, then prune.** When the ledger's chronological log grows large, fold new
  lessons into the sweep‑list and trim old entries — the detail stays in `git log`.
- **Don't store** what the agent can read from source: code, file paths, git history,
  or duplicates of `CLAUDE.md`. Memory is for *judgement*, not a copy of the repo.
- **Highest‑value memory is a user correction.** When a human corrects the agent,
  that becomes a rule — it prevents a whole class of repeat mistakes.

---

## Provenance & credit

The **file‑based, deterministic, zero‑dependency** memory idea — and the
"keep the index small or it gets silently truncated" insight — are shared with, and
partly inspired by, [`LuciferForge/claude-code-memory`](https://github.com/LuciferForge/claude-code-memory)
(MIT). This project extends that idea from *recall* toward *operational discipline*:
the bug‑class sweep‑list, the depth‑first journey method, the multi‑agent git
protocol, and the definition of done are distilled from running many agents against
one large production codebase.

## License

MIT — see `LICENSE`.
