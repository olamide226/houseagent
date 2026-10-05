# 0015 The calendar feed link is shown once, and the feed names time zones without defining them

## Context

The feed at `/ics/{token}.ics` has no login, because calendar apps cannot log in. The spec stores
only a hash of the token (`households.calendar_token_hash`) and wants a "copy ICS subscribe link"
action. A hash cannot be turned back into a link. RFC 5545 also expects a `VTIMEZONE` block for
every `TZID` a feed uses.

## Decision

- "New subscribe link" on the Calendar page generates a token, stores its SHA-256, and shows the
  link in that one response. Asking again makes a new token and the old link stops working.
- Times are written as `DTSTART;TZID=Europe/London:...` with the IANA name and no `VTIMEZONE`
  block. `X-WR-TIMEZONE` carries the same name.
- Cancelled events are left out of the feed. `DTEND` is written only when an end time is known.

## Consequences

- A leaked link is revoked by making a new one; nothing that can rebuild a link is stored.
- The link cannot be looked up later. Whoever needs it again makes a new one and re-subscribes on
  each phone.
- Apple Calendar and Google Calendar resolve IANA zone names themselves, but the feed is not
  strictly valid RFC 5545 without `VTIMEZONE`, and it has not been tested against either app.
  A stricter client may need the block added.
- An event with no end time has zero length in a calendar app.
