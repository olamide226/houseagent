# Data model

The DDL is [`schema.sql`](../schema.sql), applied verbatim by Alembic migration `0001`, and the
migrations after it: `0002` adds the household job columns to `reminders`, `0003` the two
tables for connected calendars and `events.all_day`, and `0004` the `receipts` and
`receipt_lines` tables and `households.currency`.
Table-by-table purpose is in [spec.md section 4](spec.md#4-data-model). This page covers what
milestones 1 to 5 use and the rules the code enforces.

## Tables in use

| Table | Written by |
| --- | --- |
| `households`, `members`, `locations` | `/setup` (`services/households.py`); later members by `add_family_member` and the Family page (`services/members.py`) |
| `households.onboarding_state`, `households.digest_time`, `households.currency` | `onboarding_advance`, `remember`, the Settings page |
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
| `receipts`, `receipt_lines` | `services/spending.py` only: `log_inventory` reading a receipt, `update_receipt`, the Spending page |
| `events`, `reminders` | `services/calendar.py` only; the recurrence job adds reminder rows, the reminders job updates their status |
| `reminders` with a `kind` (household jobs) | `services/jobs.py` only, from `add_job`, `update_job`, a tick on a nudge and the Jobs page; the `job_nudges` job moves a row on as it speaks |
| `households.calendar_token_hash` | the Calendar page's "New subscribe link" |
| `calendar_connections` | the Calendar page (connect, pick a calendar, disconnect) and the worker's `sync_calendars` job (`services/calendar_sync.py`) |
| `calendar_links` | `services/calendar_sync.py` only |
| `agent_actions` | every tool call or dashboard action that wrote something, each invite redemption, and each change read from a connected calendar (`source = 'calendar'`) |
| `outbox` | turns, invites and welcomes, login links, reminders, digests |
| `job_runs` | the daily brief, the weekly digest, the consumption model and the low-stock prompt, one row per household and run; `monthly_spending`, one row per household and month once that month's spending has gone out with a weekly digest; and one `worker_heartbeat` row per household that the worker moves forward every minute |
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
the rest, including a voice note's `transcript` and a file's `name` and `text` (the words read
from a PDF; an empty `text` means none could be read).

`messages.meta` on a forwarded message holds `forwarded`, and `forwarded_from` and
`forwarded_sent_at` where the channel gives them.

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
- Rows that can still share the time they are sorted by have a second sort column, so their order
  is the same every time: family members made in one turn (`created_at`, then `name`), reminders
  and jobs due at the same minute (then `text`), events that start together (then `title`).

## Receipts and spending

A receipt that is read keeps what it cost beside the stock it logged, never instead of it
([ADR 0040](adr/0040-spending-is-kept-from-receipts-beside-the-stock-log.md)).

- `receipts`: `shop` (null when it could not be read; otherwise the name the household uses for
  it), `purchased_on` (the date printed, or the household-local day it was sent when that could
  not be used), `total` (as printed, null when the receipt shows none), `currency` (the
  household's when it was read), `status` (`kept`, or `removed`: taken out of the spending, or the
  same receipt sent a second time), `source_message_id` (the photo it came in on).
- `receipt_lines`: one row per line that was logged to stock and had a printed price: `item_id`,
  `label` (the item's name then), `quantity`, `unit`, `price` (what the line cost).
- A receipt counts for `total`, or for its lines added up when `total` is null. A receipt in a
  currency other than the household's is left out of every sum.
- A stock event with source `receipt` whose message has no `receipts` row is a receipt read
  without prices. All receipts from before migration `0004` are that.
- Undoing the `log_inventory` that read a receipt deletes its rows. Correcting or removing one
  later is its own undoable action. `inventory_events` and `stock` are never touched by either.

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
  cancelled, or the reminder was dropped as stale after an outage. `acked` is only used by a
  household job (below).
- `reminders.target` is `household` when a child or more than one adult takes part, otherwise
  `member` with that adult (or whoever booked it when nobody is named).
- Cancelling an event sets `events.status = 'cancelled'`; nothing is deleted except by undo.
- `events.all_day` is an event with no time of day. `starts_at` is midnight in household time
  and `ends_at` the midnight it ends on. It has no reminder rows whatever its
  `remind_before_minutes`; the morning brief names it on its day and the day before. It counts
  as "today" all day, so it is still listed at noon. One arrives only from a connected calendar.
  The column is nullable, and null reads as false, so an undo copy of an event taken before
  migration `0003` can still be put back.

## Household jobs

A household job is a `reminders` row with a `kind` and no event
([ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)). A plain reminder has
`kind` null and the other six columns null with it.

| Column | Holds |
| --- | --- |
| `kind` | `job`: to be done and ticked off. `date`: a birthday or anniversary, only said |
| `text` | The job's name, as the family calls it |
| `member_id` | Whose job it is, a child included; null for everyone. `target` follows it, but who is told is worked out when a message is sent |
| `due_at` | The occurrence the row is waiting on, in UTC. A day with no time is stored at 09:00 household time |
| `all_day` | The job was given a day and no time |
| `rrule` | How it comes round, anchored at `due_at`; null for a job that happens once |
| `lead_days` | How many days before it is first said; 0 for none |
| `fire_at` | When the worker next has something to do: the next message about this occurrence, or the first about the next |
| `asked_at` | The first message about this occurrence; null until then. A job with this set is waiting to be ticked off |
| `done_at`, `done_by` | When it was last ticked off, and by whom |

- `status` is `scheduled` while `fire_at` is still to come, which for a repeating job is always.
  A job that happens once becomes `acked` when it is ticked off, or `sent` once everything has
  been said and nobody has. `cancelled` is a job that was stopped.
- Ticking a repeating job off moves `due_at` to the next occurrence and clears `asked_at`.
  Nothing is kept of earlier occurrences beyond `done_at` and `done_by`; the Activity page has
  each "done" as an action.
- The seven columns are nullable, so an undo copy of a reminder taken before migration `0002`
  can still be put back.
- A sent nudge's `messages.meta` has `job` (the row's id) and `due` (the occurrence, as epoch
  seconds). That is how a tick on the message, or a reply that quotes it, finds the job.

## Connected calendars

`calendar_connections` has one row per adult who has signed in to their own calendar: Google
([ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)) or Apple
([ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)), in `provider`.

- `status` is `choosing` (signed in; `choices` lists the calendars to pick from), `active`, or
  `broken` (it stopped for good and its owner was told; `last_error` says why).
- `secret` is the sign-in, encrypted with `CALENDAR_SECRET_KEY`: Google's refresh token, or for
  Apple the email and the app-specific password. It and `sync_token` are left out of the
  household export.
- `calendar_id` is the one calendar shared: Google's id for it, or for Apple the calendar's
  CalDAV address. `(household_id, provider, calendar_id)` is unique: a calendar is shared with a
  household once.
- `sync_token` is the provider's place in its list of changes, and `next_pull_at` when to ask
  again. `leased_until` is set while a worker is on the connection, and after a failure it is
  when the connection is next tried; `failures` counts failures in a row.

`calendar_links` has one row for each household event that is on a connected calendar.

- `remote_id` is the event's id on that calendar and `remote_etag` its version as last seen. On
  an Apple calendar the id is the path of the event's file; a date of a series that was changed
  by itself is in the same file and has that path, `#` and the date (`…/football.ics#20261013`).
- `synced` is the event as both sides last agreed on it: title as sent, start, end, whether it
  lasts all day, the repeat rule, the skipped dates, the place and the notes. A difference
  between it and the household's event is sent to the calendar; a difference between it and
  what the calendar reports is applied here.
- `instances` holds the dates of a series that the calendar itself keeps as a changed or deleted
  occurrence. They are skipped here like any other date and are never sent back as skipped.
  Only Google needs it: an Apple series says in its own file which dates it skips.
- `event_id` becomes null when its event row is deleted (an undo of the action that made it).
  Such a link means "remove it from the calendar", and goes once that is done.
- The link of a one-off event that ended more than 30 days ago is dropped. The event stays on
  both sides and is no longer compared.

An event read from a calendar is created, changed or cancelled through `services/calendar.py`
with an `agent_actions` row whose `source` is `calendar` and whose `member_id` is the calendar's
owner. `created_by` is that owner too, which is who its reminders go to unless its title names
someone in the family.
