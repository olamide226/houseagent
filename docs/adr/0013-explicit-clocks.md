# 0013 The clock is always passed in

## Context

Calendar behaviour depends on "now": which reminders are already past, what "tomorrow" means,
whether a digest is due. The milestone has to be shown correct across the 25 October 2026 clock
change, and the eval case "Ada has GP on Wednesday" must mean the same date whenever it runs.
Patching the clock (freezegun) freezes Python but not Postgres, and the code uses both.

## Decision

- **A turn's clock is when its message arrived.** `Ctx.now` is set from the envelope's
  `received_at`, the same moment the household brief shows as "Now". Tools read `ctx.now`.
- **Jobs take `now` as an argument** (`fire_reminders(now)`, `expand_recurrence(now)`,
  `daily_brief(now)`, `weekly_digest(now)`, `dispatch_due(adapters, now=...)`), defaulting to the
  real time. `enqueue()` takes `send_after` so a job's sends carry its clock.
- **`simulate_turn()` takes `now`**, and the eval runner pins it to Monday 5 October 2026 at 12:00
  in London, advancing a minute per turn.
- No clock-patching dependency is added.

## Consequences

- Tests step a controlled clock through weeks of worker activity in a few seconds and assert the
  exact local time of every send.
- "In two hours" counts from when it was said, even if the worker was busy or down.
- A message processed long after it arrived can create a reminder whose time has just passed in
  real terms; it is then due at once and the reminders job sends it on its next tick.
- `agent_actions.created_at` and the undo window still use the database clock. Undo is about real
  elapsed time, not the turn's clock.
