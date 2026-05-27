# Reports, Errors & User Guidance

## Reports

Reports:
- infrastructure report
- operation report
- publishing report
- creation report
- import report
- visibility report
- permission report
- failed items report
- ecosystem report
- drift report
- health report
- billing usage report
- regional deployment report

Formats:
- Telegram message
- CSV
- PDF

Reports should explain:
- what happened
- what succeeded
- what failed
- what was skipped
- what to do next

## Human errors

Do not show raw technical garbage by default.

Explain:
- what failed
- why
- affected object
- what user can do
- whether retry is possible

Examples:
- username already taken
- account has no rights
- bot token invalid
- proxy unavailable
- channel already exists
- avatar invalid
- description too long
- account not ready
- invite link could not be created
- template missing required field

Every mass operation includes:
- successful items
- failed items
- skipped items
- retry failed
- report

## User guidance rule

The user should never wonder:
- what this button does
- what happens next
- whether something started
- where the result is
- why something failed
- whether it can be undone
- how to retry

Every complex flow must:
1. explain first
2. collect input
3. show preview
4. ask confirmation
5. show progress
6. show result
7. offer retry/report/repeat
