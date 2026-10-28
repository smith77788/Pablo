# Mass Action Engine

Mass actions are the product.

Every action should support:
- single object
- multiple selected objects
- all objects in ecosystem
- objects by tag
- objects by region
- objects by language
- objects by status
- objects by template mismatch
- objects by search query

## Wave concept

User-facing term:
Wave

Wave types:
- Creation Wave
- Publishing Wave
- Update Wave
- Sync Wave
- Regional Wave
- Check Wave
- Import Wave
- Template Wave
- Repair Wave
- Permission Wave
- Visibility Wave
- Admin Assignment Wave
- Avatar Update Wave
- Command Sync Wave
- Invite Link Rotation Wave

## Every wave must support

- preview
- simulation
- estimated duration
- target summary
- affected assets
- skipped items
- conflicts
- permission problems
- progress updates
- success summary
- failure summary
- retry failed
- repeat all
- duplicate to another ecosystem
- save as template
- generate report
- history entry

## Operation simulation

Before execution, show:
- what will happen
- which assets are affected
- how many assets
- estimated duration
- possible conflicts
- missing permissions
- skipped items
- safety score
- recommendations

No large operation should start without preview and confirmation.

## Replay

Every operation supports:
- repeat
- retry failed
- repeat selected subset
- duplicate operation
- apply to another ecosystem
- scale up
- save as template
- schedule again
- compare with previous run
