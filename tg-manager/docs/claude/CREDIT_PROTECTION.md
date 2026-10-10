# Credit Protection Rules

## Budget Rule

- Maximum 20% of work time on research/reading
- Minimum 80% on actual fixes (code changes, edits, commits)

## Reading Budget

Do NOT read a file unless:
1. About to edit it
2. Specifically need a line number or value you don't have
3. Verifying a fix you just made (but prefer trusting Edit tool success)

Do NOT run grep/search unless:
1. Looking for a specific pattern you haven't seen yet
2. Finding the location of something new

## Anti-Waste Rules

- Do NOT read entire directories to "understand the codebase"
- Do NOT read files that appear in COMPLETION_REGISTRY without new signal
- Do NOT run analysis scripts unless the output is directly used in the next fix
- Do NOT create intermediate analysis documents — fix directly
- Do NOT spawn subagents for analysis only — spawn for actual parallel work

## When Root Cause is Known

If you know WHY something is broken → fix it immediately.
Do NOT:
- Write a summary first
- Create a plan document
- Read the file "to confirm" (you know it)
- Ask permission (unless destructive)

Just fix it.

## Token Efficiency

- Prefer Edit over Write (smaller diffs)
- Prefer targeted Read (with offset+limit) over full file reads
- Prefer Grep over reading whole files for pattern search
- Batch independent tool calls in parallel

## Commit Discipline

- Commit after every logical chunk (not after every file)
- Push after every commit (auto-deploy chain)
- Never accumulate more than 5 files uncommitted
