# 0001 Postgres is the source of truth

## Context

The agent must never be the only place a fact lives. Model context is lossy, an agent framework's
memory is opaque, and two adults need the same shopping list whatever channel they use.

## Decision

All state lives in Postgres. The agent reads and writes only through tools; tools and the dashboard
write only through `app/services/`. The api and worker processes communicate only through tables
(`messages`, `outbox`) plus `LISTEN/NOTIFY` for latency.

## Consequences

- Any runtime or model can be swapped without migrating state.
- Every write can be logged with its inverse, which is what makes undo possible.
- The household brief is rebuilt from the database each turn, so the model cannot drift from it.
- Deterministic rules live in code next to the data, not in the prompt.
