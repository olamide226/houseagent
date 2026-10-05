# Architecture

One container image runs as two processes that talk only through Postgres.

```mermaid
flowchart LR
    TG[Telegram] -- webhook --> API
    Browser -- dashboard --> API
    Cal[Phone calendar] -- "ICS feed" --> API
    subgraph image[one image, two processes]
        API[api: uvicorn app.main:app]
        W[worker: python -m app.worker.main]
    end
    API -- "insert messages, NOTIFY inbound" --> PG[(Postgres)]
    PG -- "received messages, pending outbox" --> W
    W -- "tool writes, outbox rows" --> PG
    W -- tool-calling loop --> LLM[LLM provider]
    W -- "replies, reminders, digests" --> TG
```

(The same diagram lives in [diagrams/architecture.mmd](diagrams/architecture.mmd).)

- **api** is stateless. It verifies and stores webhooks, serves the dashboard and the calendar
  feed, and returns fast.
- **worker** does everything slow or timed: model calls, transcription, sends, reminders, digests.
- **Postgres is the source of truth.** The agent reads and writes only through tools, and tools
  only through `app/services/`.

## Design principles

1. Postgres is the source of truth.
2. LLM for language, code for rules. Finished staple to the list, bought to restocked: code.
3. Approximate beats absent. Quantities are optional everywhere.
4. Quiet by default. A reaction instead of a reply.
5. Idempotent everywhere. Every webhook may arrive twice.
6. Everything is undoable.
7. Nothing leaves the household. The router only sends to the household's own threads and handles.

## Three types carry every message

Defined in `app/core/envelope.py`. No module outside `app/channels/` knows a provider payload shape.

| Type | Produced by | Meaning |
| --- | --- | --- |
| `InboundEvent` | a channel adapter | Channel-shaped facts: who, where, text, media, reaction |
| `Envelope` | the inbound pipeline | What the agent reads: one thread's debounced batch as text |
| `OutboundMessage` | anything that wants to send | A row in `outbox`; target is a thread, a member or the household |

## Message flow

**Webhook (api, `app/main.py`, `app/pipeline/inbound.py`).** Verify, parse, and for each event:
look up the sender in `channel_identities`. An unknown sender stores nothing and gets no reply,
unless the whole message is an invite code. A known sender's thread is upserted, the first group
thread becomes the household's primary thread, and the message is inserted with
`ON CONFLICT (thread_id, external_id) DO NOTHING`, then `NOTIFY inbound`. A bad payload is logged
and answered 200; a database error returns 503 so the provider retries.

**Processing (worker, `inbound.process_household`).** A household is ready when its newest
unprocessed message is older than `DEBOUNCE_SECONDS`, which batches "out of eggs", "and bread",
"oh and milk" into one turn. The worker then, in one transaction:

1. takes `pg_advisory_xact_lock(hashtext(household_id))`, so two messages never race on stock;
2. claims the household's `received` messages (`FOR UPDATE SKIP LOCKED`);
3. answers the exact keyword `dashboard` with a login link, without the agent;
4. for each thread: transcribes voice notes, builds the `Envelope`, runs the agent turn;
5. queues the response: nothing for `NOOP`, an `ack` reaction for `ACK`, otherwise the reply text
   (as a reply to the last message when the batch had more than one);
6. marks the messages `processed` and stores usage, tool calls and latency in `messages.meta`.

Each thread's turn runs in a savepoint. If the turn fails (for example the model is down), the
savepoint rolls back so nothing is half-recorded, the messages become `failed`, and the thread
gets "Sorry, that didn't go through, try again?" once. Within a turn each tool call has its own
savepoint, so a failing tool does not undo an earlier one. See [ADR 0009](adr/0009-one-transaction-per-turn.md).

**Sending (worker, `app/pipeline/router.py`).** Every send is an `outbox` row inserted in the
writer's transaction, so a rolled-back turn sends nothing. The dispatcher takes due rows with
`FOR UPDATE SKIP LOCKED`, resolves the destination, sends, stores an `out` row in `messages`, and
on failure backs off 10 s, 30 s, 2 min, 10 min, 30 min before marking the row `failed`.

Destinations come only from the database:

| Target | Resolves to |
| --- | --- |
| `thread` | That thread, if it belongs to the household. A DM thread must also match a verified member handle |
| `member` | The member's preferred-channel identity, then telegram, whatsapp, imessage |
| `household` | The primary thread if set, else one send per adult |

A row with no allowed destination is marked `failed` and never sent.

**Quiet hours.** Each member has a quiet window, 21:30 to 07:00 by default; a window whose start
is later than its end crosses midnight. Before a send, the dispatcher asks whether the recipient
is inside theirs. If so the row is not sent: its `send_after` moves to the end of the window and
it is picked up again then. A send to a group waits while any adult is in quiet hours. Two kinds
of row are never held: `urgency = 'high'`, and rows with `respect_quiet_hours = false`, which is
what direct replies, invite welcomes and login links use. See
[ADR 0012](adr/0012-quiet-hours-and-reminder-delivery.md).

## Scheduled jobs

The worker runs each job as its own supervised task (`app/worker/jobs.py`). Every job takes the
current time as an argument, so tests drive them on a controlled clock.

| Job | Every | What it does | Why a second run is harmless |
| --- | --- | --- | --- |
| `inbound` | NOTIFY, or 2 s | Turns, as above | Messages are claimed with `SKIP LOCKED` under the household lock |
| `outbox` | 2 s, or when woken | Sends due rows | `SKIP LOCKED`; a sent row is no longer pending |
| `fire_reminders` | 15 s | Due reminders become outbox rows. A one-off becomes `sent`; a repeating one moves to its next time | `SKIP LOCKED`, and the outbox dedupe key is `reminder:{id}:{fire_at}` |
| `expand_recurrence` | 1 h | Reminder rows for each recurring event's occurrences in the next 48 hours, skipping exception dates | Unique `(event_id, fire_at)` |
| `daily_brief` | 1 min check | At the household's `digest_time`: today's events and reminders, items to use within 2 days. Nothing on an empty day | A `job_runs` row per household and date |
| `weekly_digest` | 1 min check | Sunday 18:00: the week ahead, the list count, low and expiring items | A `job_runs` row per household and ISO week |

A reminder is sent within about 17 seconds of its `fire_at` (15 s tick, then the outbox, which the
job wakes). A digest missed by more than four hours is skipped instead of being sent late. After
an outage, a reminder is dropped rather than sent if a later reminder for the same event is also
due, or if the event began more than ten minutes ago.

## Repo layout

```text
app/
  main.py        create_app(): webhook route, health, dashboard and feed routers
  config.py      Settings, logging
  db.py          engine, tx(), advisory lock, query helpers
  core/          envelope types, identity and invite codes, time, quiet hours, recurrence
  llm/           neutral types, OpenAI-compatible and Anthropic adapters, speech-to-text
  channels/      ChannelAdapter protocol, registry, Telegram
  pipeline/      inbound (persist, debounce, turn, simulate_turn), media, router
  agent/         runtime interface, loop, prompt, resolve, actions (undo), stock, tools/
  services/      the one write path, shared by tools and dashboard
  dashboard/     auth, routes, templates, static
  ics/           the read-only calendar feed
  worker/        supervisor and jobs
tests/           unit/ contract/ evals/
```

## Not built yet

The WhatsApp 24-hour window, falling back to a member's next channel after a failed send, and
`MediaStore` arrive with the milestones that need them (WhatsApp, iMessage, photos). The jobs
`consumption_model`, `low_stock_prompt`, `imessage_health` and `media_cleanup` are later
milestones too, so the weekly digest lists low and out items but not predicted ones.
