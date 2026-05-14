# Fixer Agents

This directory contains targeted fix agents dispatched by the Smart Orchestrator
during the **Fix Phase**. Each fixer handles a specific file or domain.

## Available Fixers

| File | Module | Target |
|------|--------|--------|
| `fixer-bot.js` | `./fixer-bot` | `bot.js` — Telegram bot logic |
| `fixer-api.js` | `./fixer-api` | `routes/api.js` — REST API routes |
| `fixer-db.js`  | `./fixer-db`  | `database.js` — DB schema & queries |
| `fixer-frontend.js` | `./fixer-frontend` | `public/` — HTML/CSS/JS |

## Interface Contract

Each fixer module must export:

```js
module.exports = {
  name: 'Fixer: Bot',          // human label
  canFix(finding) { ... },     // returns true if this fixer handles the finding
  async fix(finding) { ... },  // applies the patch, returns { ok, msg }
};
```

## How the Smart Orchestrator Uses Fixers

1. After the **Analysis Phase** collects all CRITICAL/HIGH findings, the
   **Discussion Phase** proposes fix strategies for each.
2. The **Consensus Phase** ranks strategies and builds a fix plan.
3. The **Fix Phase** calls `getFixer(finding.file)` and dispatches the right fixer.
4. The **Verification Phase** re-runs only the agents that reported the fixed findings.
5. Results are recorded in `agent_findings` for future cycle learning.
