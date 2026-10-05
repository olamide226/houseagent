# 0014 Recurring events: local-time rules, exceptions as a date plus a one-off

## Context

"Chatterbox, Tuesdays at 9" must stay at 09:00 local when the clocks change. People cancel or
move a single week. Models produce times and RFC 5545 rules with small variations: an offset that
is right today but wrong after a clock change, an `UNTIL` without a time zone, a start date that
is not on the rule's weekday, `COUNT` on a reminder.

## Decision

- **Rules are expanded from a time-zone-aware start in household time** with `python-dateutil`
  and `zoneinfo`. The UTC instant of each occurrence follows from that.
- **The tools ask for local time without an offset.** A naive time is household time. An offset,
  if the model sends one, is honoured as the spec says.
- **The stored rule is normalised:** upper case, no `RRULE:` prefix, `UNTIL` in UTC (a date-only
  `UNTIL` means the end of that local day). Frequencies shorter than hourly are refused.
- **A series starts on its first real occurrence.** "Every Tuesday, starting today" said on a
  Monday is stored with Tuesday's start, so the feed's `DTSTART` agrees with its rule.
- **One occurrence is changed by adding its local date to `exdates`.** If it moved or changed
  rather than being cancelled, a separate one-off event is created with the change.
- **`modify_event` gained an optional `occurrence` date** so a week other than the next one can
  be skipped. The spec's signature had no way to say which occurrence.
- **Repeating standalone reminders keep only `fire_at` and the rule.** After each send the next
  time is computed from the last one. `COUNT` is refused for them, since the count would restart
  every time; an end is given with `UNTIL`.

## Consequences

- The clock-change tests pass without special cases, in autumn and spring.
- A moved occurrence is an ordinary event afterwards: it can be edited, cancelled and undone, and
  appears in the feed with its own UID while the series carries the `EXDATE`.
- An event that genuinely happens in another time zone works only if the model sends its offset.
- Changing a series' start with scope `all` re-anchors it; a rule with `COUNT` then counts again
  from the new start.
