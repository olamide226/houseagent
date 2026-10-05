# 0004 One api replica, scale by configuration

## Context

One household generates a few dozen messages a day. Running several replicas from the start adds
cost and coordination for no benefit, but the design should not prevent scaling.

## Decision

Run one api replica and one worker replica. The api is stateless; the worker is replica-safe by
construction: `FOR UPDATE SKIP LOCKED` on message and outbox claims, and a per-household advisory
lock around each turn.

## Consequences

- Scaling out is a replica count, not a code change.
- Channels retry failed webhooks, so a single-replica restart loses nothing.
- One piece of state is per-process today: the invite-attempt rate limit is counted in the api's
  memory. It must move to Postgres before the api runs with more than one replica.
