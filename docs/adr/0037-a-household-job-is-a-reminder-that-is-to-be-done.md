# 0037 A household job is a reminder that is to be done

## Context

A household has things that come round or fall due: the bins every Tuesday night, the MOT on a
date, a prescription every 28 days, a birthday. Each belongs to someone, wants saying at a
sensible time, and for most of them somebody then has to say it is done.

What was built already covered half of that. A standalone reminder has a text, a person or the
household, a time, and an RRULE that moves `fire_at` on after each send. Missing were: a due
date that is not the moment of the message, any record that the thing was done (the status
`acked` existed and nothing set it), a way to say "later", and a second ask for what was left.

Three shapes were weighed.

- **An event of kind `task`** with its reminder rows. The calendar, the feed and `modify_event`
  would come free. But an event's reminders are planned by lead time from its start, dropped
  when its start has passed, and matched to the plan by fire time. A second ask after the due
  time and a "later" at any time go against all three, and an event has no day without a time.
- **A table of its own.** A second recurrence engine and a second sender beside the first.
- **A standalone reminder with more columns.** The sender, the outbox, quiet hours, undo and the
  export already carry it. This is what was built.

## Decision

**A job is a `reminders` row with a `kind`**, `job` or `date`. A plain reminder has none, and
nothing that reads or sends reminders sees a row that has one. Migration `0002` adds seven
columns: `kind`, `due_at` (the occurrence it is waiting on), `all_day`, `lead_days`, `asked_at`,
`done_at` and `done_by`. `member_id` is the owner, or nobody for everyone. The rules are in
`app/services/jobs.py`; "jobs" there is the family's word, and the worker's scheduled jobs are
another thing with the same name.

**`fire_at` stays what it is for every reminder: when the worker next has something to do.** For
one occurrence that is, in order:

| Said | When | For |
| --- | --- | --- |
| It is coming | 09:00, `lead_days` before | A job due on a day that comes round less than every 90 days or only once: 14 days. A date: 7 days. Otherwise none, unless the person says how long before |
| It is due | At its time, or 09:00 for a day with no time | Every job and date |
| It is still to do | 09:00 the morning after | A job nobody has ticked off. Once, and never for a date |

After the last of them the row waits for the next occurrence's first message, and begins that
occurrence when it comes. A job that repeats therefore stays `scheduled` for its whole life, as
a repeating reminder does. A job that does not ends `acked` when it is ticked off, or `sent` when
everything was said and nobody did.

- **The morning-after ask is skipped when the next occurrence speaks first.** A job at 08:00
  every day is said once a day.
- **A time somebody picked is meant** ([ADR 0012](0012-quiet-hours-and-reminder-delivery.md)): a
  job's own time of day and a "later" with a time go out inside quiet hours. The 09:00 messages
  wait for quiet hours to end.
- **Who is told**: the owner in their own chat if they are a connected adult. Otherwise everyone,
  with the owner named: a child's job, or one that is nobody's.
- **A date has nothing to tick off** and comes round every year whether or not anyone says so.

**Done, later, skip, change and stop** are one tool, `update_job`, and the same functions behind
the Jobs page.

- **Done** moves a repeating job to its next occurrence and records who and when. The schedule
  is fixed: a prescription done three days late is next due 28 days after it was due.
- **Done said twice is not next week's.** When a job has not been asked about yet and is more
  than half its cycle away, "done" changes nothing and the result says when it was last ticked
  off. A job is often mentioned again after it was ticked. The Jobs page's circle is not held to
  this: a tap is meant.
- **Later** is two hours on for a job with a time on its day, the next morning otherwise, or the
  time the person gives, and never past the next occurrence's own message.
- **Which job** is the one named, by trigram match as for events, with words such as "the" left
  out so "the car" does not find "Put the bins out". With no name it is the job whose message
  the person is answering, else the only one waiting. Two that fit equally are a question.

**A thumbs-up or a tick on the nudge is done, in code.** The outbox key of a nudge names the job
and the occurrence, and the router keeps both on the sent message. A reaction from the set in
`TICKS` on such a message ticks that occurrence off without a model call, and the assistant puts
its own thumbs-up on the nudge. It is one tap and it means one thing. A tick on the nudge of an
occurrence already ticked off changes nothing, so two adults can both tick it in the group.
Telegram has no tick among its reactions, so the thumbs-up counts.

**"Remind me" stays a reminder.** "Remind us every Sunday at 6pm to put the bins out" is
`set_reminder`, said once and never asked about again. A job is what someone describes: whose it
is, when it falls due, how often. Both tool descriptions say so, because without it the model
sent that sentence to `add_job` in 4 runs of 6.

**The new columns are nullable.** Undo puts a row back from the copy taken before a change, and
a copy taken before the migration has no value for them.

## Consequences

- One sender, one quiet-hours rule, one undo, one export. A job is in the brief, in
  `list_upcoming` and on Today beside events and reminders.
- Jobs are not in the calendar feed, the morning brief or the weekly digest. The nudges are how
  they are said.
- A family that never answers "done" to a weekly job gets two messages a week for it: the nudge
  and the morning after. That is the "once, gently" that was asked for.
- A job counted from when it was last done ("every three months after the last time") cannot be
  said. Every repeat is a fixed schedule.
- Wording is written when a message is sent, not when the job is set, so it is right for the
  day it is read. A 09:00 message held until quiet hours end is still read on its day.
- `reminders.status = 'sent'` now has a second meaning for a row with a `kind`: all said, not
  done. `acked` is used for the first time.
- After the worker has been away, a job says one thing about the occurrence that is current,
  not one per occurrence missed.
- A nudge that failed on one chat app and was sent again on the person's next one does not
  carry the job on that second copy. A tick on it goes to the agent, as any reaction does.
- Undoing a "done" after the worker has moved the row on puts the row back as it was when it was
  ticked, and the worker picks it up from there.
