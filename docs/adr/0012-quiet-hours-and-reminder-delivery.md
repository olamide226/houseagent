# 0012 Quiet hours are enforced by the router, and reminders are planned around them

## Context

Reminders must fire within a minute of their time and hold during quiet hours unless urgent. The
spec puts deferral in the router, skips the day-before reminder when it lands in quiet hours, and
says a reminder "the user explicitly set inside quiet hours" bypasses them. It leaves open what
happens to an hour-before reminder for an early event, what a held reminder should say, and what
to do with reminders that come due while the worker is down.

## Decision

- **One place holds sends.** The reminders job always queues a due reminder on time. The outbox
  dispatcher checks the recipient's quiet hours and moves `send_after` to the end of the window.
  A group send waits while any adult is in quiet hours; `urgency = 'high'` and
  `respect_quiet_hours = false` are never held.
- **The plan is made when the event is written** (`services/calendar.py`), per reminder:
  - a day-before reminder that lands in quiet hours is not created; the morning brief lists that
    event under "Tomorrow" instead;
  - a shorter one that lands in quiet hours is left to be held, and worded for when it will be
    read ("today", not "tomorrow", if it is read the next morning);
  - unless holding it would deliver it after the event began, in which case it is `high`.
- **A standalone reminder set for a time inside the target's quiet hours is `high`.** Someone who
  says "remind me at 11pm" means 11pm.
- **Stale reminders are dropped, not sent.** After an outage a due reminder is cancelled if a later
  reminder for the same event is also due, or if its event began more than ten minutes ago.

## Consequences

- "Remind me at 11pm" works without the model having to set `urgent`.
- An early flight still gets its hour-before reminder; an 07:30 school run gets it at 07:00.
- The wording is fixed when the reminder is planned. If someone later changes their quiet hours,
  a held reminder can say "tomorrow" on the day itself until the event is next edited.
- Quiet hours are read per recipient at send time, so changing them takes effect immediately for
  holding, even though wording does not follow.
- A reminder sent late but before its event keeps its original wording.
