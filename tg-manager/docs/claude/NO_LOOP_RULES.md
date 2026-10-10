# No-Loop Rules

## What Loops Waste

Each loop costs credits and time. One revisit of a known module = one new feature NOT fixed.

## Hard Rules

1. **Never revisit a module in COMPLETION_REGISTRY** without a new defect or regression
2. **Never re-read a file** that was read in the same session without a specific reason (missing line, edit confirmation)
3. **Never audit a module** that is already WORKING or VERIFIED in MODULE_STATUS
4. **Never create a roadmap** when the task is to fix a specific defect
5. **Never re-analyze** a problem that already has a known root cause

## Loop Detection

If you find yourself:
- Reading the same file for the 2nd time in a session → STOP, use what you already know
- Writing a summary of what's wrong → STOP, fix it instead
- Listing all problems in a module → STOP, fix the highest-priority one
- Re-running grep on a module you already analyzed → STOP, you have the data

## The Anti-Loop Test

Before any action ask: "Have I done this or seen this before in this session?"

If YES → use the existing result, do not redo.

## Familiar ≠ Important

The fact that a module was discussed many times does NOT make it higher priority.

Priority is set by CURRENT_MISSION.md — not by session history.

## After Completing a Module

1. Update MODULE_STATUS.md
2. Add to COMPLETION_REGISTRY.md if fully done
3. Move to the NEXT unclosed item in CURRENT_MISSION.md
4. Never go back
