# Data model

The full DDL is [`schema.sql`](../schema.sql), applied verbatim by Alembic migration `0001`.
Table-by-table purpose is in [spec.md section 4](spec.md#4-data-model). This page covers what
milestones 1 to 5 use and the rules the code enforces.

## Tables in use

| Table | Written by |
| --- | --- |
| `households`, `members`, `locations` | `/setup` (`services/households.py`); later members by `add_family_member` and the Family page (`services/members.py`) |
| `households.onboarding_state`, `households.digest_time` | `onboarding_advance`, `remember`, the Settings page |
| `members.quiet_start`, `quiet_end`, `preferred_channel`, the invite hash | `remember`, the Settings and Family pages |
| `household_facts` | `remember`, the Settings page (`services/households.py`) |
| `places` | shops named in `remember` (kind `store`), the Settings page, and a presence ping naming a place for the first time (kind `other`, or `home` for Home) |
| `members.presence_token_hash` | the presence step of setup, an adult connecting after it, the Settings page |
| `presence_events` | the presence endpoint, one row per accepted ping |
| `nudge_log` | the presence rules and the low-stock prompt: one row per nudge key, `sent_at` moved on each time it is sent again |
| `consumption_profiles` | `services/consumption.py`, from the nightly job, the low-stock prompt and the weekly digest |
| `channel_identities` | invite redemption (`core/identity.py`) |
| `login_tokens` | the `dashboard` keyword (`services/members.py`) |
| `threads`, `messages` | inbound pipeline; the router adds `out` rows; the Chat apps page adds a group's thread |
| `households.primary_thread_id` | the first group message, a created group once confirmed, the Chat apps page |
| `items` | resolution (new items, learned aliases), dashboard item edits |
| `inventory_events`, `stock` | `services/inventory.py` only |
| `shopping_list_items` | `services/shopping.py`, the inventory side-effect rules, and the consumption model's guesses (`reason = 'predicted'`) |
| `events`, `reminders` | `services/calendar.py` only; the recurrence job adds reminder rows, the reminders job updates their status |
| `households.calendar_token_hash` | the Calendar page's "New subscribe link" |
| `agent_actions` | every tool call or dashboard action that wrote something, and each invite redemption |
| `outbox` | turns, invites and welcomes, login links, reminders, digests |
| `job_runs` | the daily brief, the weekly digest, the consumption model and the low-stock prompt, one row per household and run; and one `worker_heartbeat` row per household that the worker moves forward every minute |
| `households.letta_agent_id` | the Letta runtime, when it creates the household's agent |

Every table in the schema is now written by something.

`nudge_log.dedupe_key` is `store:{member id}:{place id}` (a shop list, again after 2 hours),
`out:{member id}:{local date}` (the "out and about" offer, once), `low_stock:{item id}` (asked
about in the 17:30 prompt, again after 3 days) or `imessage_outage` (held by a household that
uses iMessage for as long as BlueBubbles does not answer; `sent_at` is when the outage began).

`consumption_profiles.predicted_runout_at` is null unless the item has two or more measured
cycles and has been bought since it last ran out. `samples` counts the cycles that were measured;
`avg_days_to_finish` is their exponentially weighted mean ([presence.md](presence.md#predictions)).

`households.onboarding_state` is `{"step": "shops", "done": ["family", "routines"], "skipped":
["routines"]}`. `step` is the first of `family`, `routines`, `shops`, `staples`, `tour`, `rhythm`, `presence`
not yet done, and `null` when setup is complete.

`messages.media` is a list of `MediaRef`. A stored attachment has `storage_backend` and
`storage_key` (S3: `{household}/{message_id}/{n}.{ext}`; ImgBB: the image id, plus `storage_url`
and `delete_url`). Retention removes those four fields after `MEDIA_RETENTION_DAYS` and leaves
the rest, including a voice note's `transcript`.

`threads.channel` is free text. Besides real channels it holds `playground`: the threads the
dashboard's Practice chat and the eval suite talk on. Their outbox rows have status `simulated` and
are never sent.

`channel_identities.handle` is a Telegram user id, or for WhatsApp the business-scoped user id
(`GB.13491208655302741918`), with the phone number in E.164 only for a payload that has no user
id ([ADR 0019](adr/0019-whatsapp-members-are-identified-by-user-id.md)). A DM thread's
`external_thread_id` is that handle. A WhatsApp group that has been asked for but not yet
confirmed is a thread `pending:{subject}`.

`outbox.channel_used` is the channel a row was sent on, or tried on if it failed. A row whose
`dedupe_key` is `fallback:{outbox id}` is the second try, on another channel, of the send with
that id. An `out` message sent as a WhatsApp template has `meta.template`.

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
- Adding an item that is on the list only as a guess (`reason = 'predicted'`) turns that entry
  into a real one with the new reason; the list still has one active row per item.
- "Got everything on the list" ticks off what was asked for and leaves the guesses.

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

## Calendar rows

- `events.starts_at` and `ends_at` are UTC. A recurring event keeps its RRULE as text and is
  expanded from a start in household time, so "Tuesdays at 09:00" stays at 09:00 when the clocks
  change. The stored start of a series is its first real occurrence.
- `events.exdates` holds skipped occurrences as household-local dates. Changing one occurrence of
  a series adds its date here and, if it moved or changed, creates a separate one-off event.
- A one-off event has its reminder rows from the moment it is created. A recurring event has rows
  only for occurrences in the next 48 hours; the hourly recurrence job adds the rest.
- `reminders.event_id` is null for a standalone reminder. Only standalone reminders use
  `reminders.rrule`; after each send `fire_at` moves to the next occurrence.
- `reminders.status`: `scheduled`, then `sent`. `cancelled` means the event or that occurrence was
  cancelled, or the reminder was dropped as stale after an outage. `acked` is not used yet.
- `reminders.target` is `household` when a child or more than one adult takes part, otherwise
  `member` with that adult (or whoever booked it when nobody is named).
- Cancelling an event sets `events.status = 'cancelled'`; nothing is deleted except by undo.
