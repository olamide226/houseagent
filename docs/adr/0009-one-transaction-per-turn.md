# 0009 One transaction per household turn, savepoints inside

## Context

Turns for one household must not race on stock, a tool failure must not undo an earlier tool call,
the Playground needs a dry run, and a crashed worker must not leave half-recorded state.

## Decision

The worker processes a household's waiting messages in one transaction that holds
`pg_advisory_xact_lock(hashtext(household_id))`. Inside it:

- each thread's turn runs in a savepoint;
- each tool call runs in its own nested savepoint;
- outbox rows are inserted in the same transaction as the writes they announce.

If a tool fails, only its savepoint rolls back and the model is told. If the turn itself fails
(the model call errors, or anything unexpected), the turn's savepoint rolls back, the messages are
marked `failed`, and one apology is queued. If the process dies, the whole transaction rolls back
and the messages are still `received`.

The Playground and the eval suite call the same `run_turn` through `simulate_turn()` on a
connection they control: commit to apply, roll back for a dry run.

## Consequences

- A failed turn records nothing, so "try again" cannot double-count.
- A message that reliably breaks a turn is marked `failed` once instead of being retried forever.
- A database connection stays open for the length of the model call. Fine for a household; a
  deployment serving many households would need a pool sized for concurrent turns.
- The `processing` status is only ever visible inside the turn's own transaction.
- Rows written in a turn use `clock_timestamp()`, because `now()` is constant within a transaction.
