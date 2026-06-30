# Auto-Discovery, Auto-Registration & Infrastructure Graph

## Core principle

Infragram is not only a creator.

Infragram is a living registry and control layer.

If a connected account owns/administers something, Infragram should detect it, register it, classify it, and make it manageable.

## Auto-discovery

After connecting accounts/bots, Infragram detects:
- existing channels
- existing groups
- existing chats
- existing bots
- administered assets
- ownership relationships
- permissions
- linked accounts
- linked bots
- ecosystem relationships
- unmanaged assets
- duplicate assets
- missing setup

## Existing infrastructure scan

Feature:
“Scan my Telegram infrastructure”

After scan:
- show found assets
- group by type
- suggest ecosystems
- suggest tags
- detect duplicates
- detect missing metadata
- detect unmanaged assets
- allow one-click import

## Post-creation auto-linking

Whenever Infragram creates anything, it must immediately:
1. Register it.
2. Link creator account.
3. Check permissions.
4. Assign ecosystem.
5. Apply tags.
6. Apply template/DNA.
7. Add to history.
8. Enable mass actions.
9. Include in Check Everything.
10. Include in reports/visibility where needed.

No created asset should remain invisible.

## Infrastructure graph

Infragram understands:
- which account created which channel
- which account owns which group
- which account administers which channel
- which bot belongs to which channel/group/ecosystem
- which channel belongs to which ecosystem
- which group belongs to which ecosystem
- which accounts can act inside which assets
- which assets are unmanaged
- which assets lost permissions
- which assets are duplicates
- which assets are broken

Used for:
- smart targeting
- permission checks
- safe operations
- reports
- recommendations
- ecosystem health
- visibility tracking

## Unmanaged Assets Center

Section:
“Unmanaged Assets”

Shows:
- channels without ecosystem
- groups without template
- bots without commands
- accounts without role
- assets without tags
- assets with missing permissions
- assets not checked

Actions:
- organize selected
- apply template
- assign ecosystem
- assign role
- check permissions
- ignore/archive
