# Database Governance

Avoid uncontrolled schema growth.

Before adding models or migrations:

1. Inspect existing models.
2. Inspect naming conventions.
3. Inspect existing metadata / audit / operation tables.
4. Check whether an existing relation can be extended.
5. Prefer generic operation-linked structures over one-off tables.

## Prefer

- normalized relations
- reusable metadata fields
- operation-linked audit records
- migration-safe changes
- backward-compatible columns
- explicit indexes for search/filter fields
- clear ownership/workspace isolation
- encryption for secrets

## Avoid

- duplicated entity storage
- feature-specific tables that cannot be reused
- hidden JSON blobs for critical queryable data
- migrations that destroy existing data
- irreversible destructive migrations without a backup path
- secrets in plain text

## Sensitive data

Encrypt or otherwise protect:

- Telegram sessions
- bot tokens
- proxy credentials
- payment metadata where applicable
- operational secrets

No secrets should be exposed in frontend or logs.
