# 0028 A degraded channel and the next identity

## Context

The spec: "the worker calls `GET /api/v1/ping` every 5 minutes; when it fails, the router marks
the adapter degraded and falls back to each member's next identity, and DMs the admin member once
per outage." It does not say where the flag lives, what "falls back" covers beyond a
member-targeted send, or how "once per outage" is remembered.

## Decision

- **The flag is on the adapter object in the worker** (`HealthChecked.degraded`), set by the
  `imessage_health` job from one ping. The router runs in the same process and reads it. Each
  worker replica pings for itself. One failed ping degrades; one good ping restores.
- **The outage is also a `nudge_log` row** (`imessage_outage`) in each household that has an
  iMessage identity. Claiming it is what makes the admin's message once per outage across
  restarts and replicas; deleting it on recovery arms the next outage. The Channels page, which
  is rendered by the api process, reads that row to say since when.
- **What moves while a channel is degraded:**
  - a `member` send goes to the member's next identity, preferred channel first among the
    healthy ones;
  - a text for a DM thread on the degraded channel goes to that thread's owner the same way;
  - a `household` send whose primary thread is on the degraded channel becomes one send per
    adult, as when there is no primary thread.
- **What does not move:** an ack (it means nothing in another chat), a send to a group thread,
  and anything for someone whose only identity is on the degraded channel. Those are tried and
  retried where they are.
- **`members.preferred_channel` is never written by this.** Recovery needs no clean-up.
- **The admin's warning respects quiet hours**, and a warning still held when the outage ends is
  cancelled.

## Consequences

- An outage costs at most five minutes of failed sends, which the usual backoff then re-resolves
  to the next channel on their next try.
- The move goes through `_destination`, the one place that turns an outbox row into a recipient,
  so it can only land on a verified identity of the same member. The allowlist property test
  runs with iMessage degraded.
- A warning to an admin whose only channel is iMessage cannot arrive. The Channels page still
  shows the outage.
- A household whose members all use only iMessage gets nothing until the Mac is back.
- A flapping Mac sends one warning per flap, at most one every ten minutes. There is no
  hysteresis.
- BlueBubbles can answer pings while Messages is signed out. Then nothing is degraded and each
  send fails five times before the fallback of ADR 0022 moves it.
- No schema change: the state fits `nudge_log`.
