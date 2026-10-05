# Data model

The full DDL is [`schema.sql`](../schema.sql), applied verbatim by Alembic migration `0001`.
Table-by-table purpose is in [spec.md section 4](spec.md#4-data-model). This page covers what
milestone 1 uses and the rules the code enforces.

## Tables in use

| Table | Written by |
| --- | --- |
| `households`, `members`, `locations` | `/setup` (`services/households.py`) |
| `channel_identities` | invite redemption (`core/identity.py`) |
| `login_tokens` | the `dashboard` keyword (`services/members.py`) |
| `threads`, `messages` | inbound pipeline; the router adds `out` rows |
| `items` | resolution (new items, learned aliases), dashboard item edits |
| `inventory_events`, `stock` | `services/inventory.py` only |
| `shopping_list_items` | `services/shopping.py`, and the inventory side-effect rules |
| `agent_actions` | every tool call or dashboard action that wrote something |
| `outbox` | turns, invite welcomes, login links |

Not written yet: `household_facts`, `consumption_profiles`, `events`, `reminders`, `places`,
`presence_events`, `nudge_log`, `job_runs`.

`threads.channel` is free text. Besides real channels it holds `playground`: the threads the
dashboard Playground and the eval suite talk on. Their outbox rows have status `simulated` and
are never sent.

## Stock transitions

`app/agent/stock.py` is a pure function `apply(stock_row, event, item) -> stock_row`.

| Event | qty_estimate | status |
| --- | --- | --- |
| `added`, `restocked` | `qty + quantity` if both known, else unchanged (null stays null) | `in_stock` |
| `used` | `max(qty - quantity, 0)` if both known, else unchanged | recomputed |
| `low` | unchanged | `low` |
| `finished` | `0` | `out` |
| `discarded` | as `used`; no quantity means `finished` | recomputed |
| `adjusted` | `quantity` (absolute); none means unchanged | `in_stock` unless quantity is 0 |

Recompute: `out` if qty is 0; `low` if the item has a `low_threshold` and qty is at or below it;
`in_stock` if qty is above 0; otherwise the previous status (`unknown` for a fresh row).

`expires_on` follows the newest event that states one and clears when the row goes `out`.

A change with no stated location goes to the one place the item is stocked, else the item's
default location, else `store`.

## Side-effect rules (code, not prompt)

- `finished`, or `discarded` to zero, on a staple: add to the list, reason `finished`.
- `low` on any item: add to the list, reason `low`.
- `restocked` or `added`: the item's active list entry becomes `bought`.
- After two completed restock-to-finished cycles an item becomes a staple (and goes on the list).
- Ticking an item off the list logs `restocked` with source `shopping` at the usual location.
- Ticking an item off in the same turn that already logged its restock records nothing more: one
  purchase is one restock.

## Invariants

- `stock` is written only in the transaction that appends the `inventory_events` row.
- `inventory_events` is append-only. Undo adds an `adjusted` event with source `undo`.
- **Live events** are events that are not undo markers and do not belong to an undone action.
  `services.inventory.rebuild_stock()` replays live events in `occurred_at` order and reproduces
  `stock` exactly; a test checks this over random histories that include undos.
  See [ADR 0010](adr/0010-undone-events-are-excluded-from-replay.md).
- One active shopping row per item (`shopping_one_active_idx`).
- Every household has locations `fridge`, `freezer`, `store`. A new item with no location goes to `store`.
- Invite codes, login tokens and other secrets are stored as SHA-256 hashes.
- Timestamps are `timestamptz` in UTC; household time is used only at the edges (the brief, pages).
- Rows written inside one turn use `clock_timestamp()`, not `now()`, so their order is real
  even though the turn is one transaction.

## Known limit

Editing an item's `low_threshold` changes how a replay classifies old `used` events, so a rebuild
after such an edit can differ from live stock for that item.
