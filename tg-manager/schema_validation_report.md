# Schema Validation Report

**Date**: 2026-07-05 00:55:12

## Summary

| Metric | Count |
|--------|-------|
| Total files checked | 137 |
| Files with errors | 0 |
| Files with warnings only | 0 |
| Files OK | 137 |
| Total errors | 0 |
| Total warnings | 0 |

## Verdict

ALL 137 schema files PASS validation.
No syntax errors, missing PRIMARY KEYs, missing IF NOT EXISTS, or duplicate index names found.
## Validation Criteria

1. **SQL syntax correctness** — matched parentheses, no typos in keywords
2. **CREATE TABLE IF NOT EXISTS** — all CREATE TABLE must use IF NOT EXISTS
3. **CREATE TABLE PRIMARY KEY** — all CREATE TABLE must define a PRIMARY KEY
4. **CREATE INDEX IF NOT EXISTS** — all CREATE INDEX must use IF NOT EXISTS
5. **Duplicate index names** — no duplicate index names within a single file
6. **ALTER TABLE ADD COLUMN IF NOT EXISTS** — ADD COLUMN must use IF NOT EXISTS

*Note: ALTER TABLE ADD CONSTRAINT and ALTER COLUMN do not support IF NOT EXISTS in PostgreSQL.*
