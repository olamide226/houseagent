# 0010 Undone events are excluded from replay

## Context

`inventory_events` is append-only and `stock` must be rebuildable from it. Undo restores stock rows
to their exact prior state, including states no event can express: an unknown quantity, or a `low`
status with no count. The spec has undo append an `adjusted` event with source `undo`, but replaying
that event through the transition table does not always reproduce the restored row, and a stock
row the undone action created would reappear.

## Decision

Define **live events** as events that are not undo markers (`source <> 'undo'`) and are not listed
in the `touched` rows of an undone action. Replay, and the staple cycle count, use live events
only. The `adjusted`/`undo` event is still appended as an audit record of what was restored.

## Consequences

- `rebuild_stock()` reproduces live `stock` exactly after any mix of changes and undos; a test
  checks this over random histories.
- An undone "finished" does not count towards making an item a staple.
- Correctness relies on undo refusing when a later action touched the same rows, which it does.
- `agent_actions.touched` records every appended event id, so it must be kept as long as the events.
