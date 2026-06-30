# Architecture Governance

This file strengthens the original Infragram memory. It does not replace earlier files.

## Core rule

Before creating any new:
- service
- handler
- menu
- database model
- worker
- queue
- operation type
- abstraction
- utility module

inspect whether an existing system can be reused or extended.

Prefer extending reusable systems over adding isolated one-off logic.

## Avoid architecture chaos

Do not create:
- duplicate handlers for the same user flow
- operation-specific execution pipelines
- copy-pasted Telegram keyboards
- isolated database tables for tiny feature fragments
- parallel service layers doing the same thing
- `v2`, `new`, `final`, `fixed` style files
- hidden side effects inside UI handlers

## Required architecture questions

Before implementation, answer internally:

1. Where does this belong in the current architecture?
2. What existing abstractions already exist?
3. Can this be expressed as an Operation?
4. Can this reuse targeting, templates, reports, retries, and audit logs?
5. Does this reduce or increase complexity?
6. What future features will reuse this?
7. What can fail?
8. How will the user recover?

## Preferred direction

Build Infragram around reusable layers:

- Assets
- Ecosystems
- Targets
- Templates / DNA
- Operations
- Waves
- Safe Execution
- Reports
- History
- Retry Failed
- Drift Detection
- Visibility
- Billing / limits
- Governance

Do not build Infragram as a pile of unrelated buttons.
