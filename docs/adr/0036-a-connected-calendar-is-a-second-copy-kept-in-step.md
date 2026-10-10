# 0036 A connected calendar is a second copy of the household's events, kept in step by the worker

## Context

The calendar feed ([ADR 0015](0015-ics-feed-token-and-time-zones.md)) shows the household's plans
in a phone's calendar app and takes nothing back. A couple already has a calendar they both look
at, and a plan made there never reached the assistant: no reminder, no morning brief. The request
was for plans to go both ways, with Google and with Apple.

What the two offer, as their documentation said in October 2026:

- **Google Calendar API v3.** OAuth 2.0 for a web server, with a refresh token for offline
  access. `events.list` returns a `nextSyncToken`; sent back as `syncToken` it returns only what
  changed, deletions included, and a `410` means the token is forgotten and everything must be
  read again. A push channel (`events.watch`) can call a public HTTPS address when something
  changes; a channel expires and has to be replaced.
- **Apple iCloud.** CalDAV, signed in with the Apple ID and an app-specific password. There is no
  OAuth and no push to a server.

No Google or Apple credentials existed when this was built, so nothing here has been run against
either service.

## Decision

**Postgres stays the source of truth, and a connected calendar is a second copy.** Each adult
may connect one calendar. The household's `events` are the hub: a change read from Ola's
calendar is applied here, and from here it reaches Ada's.

- **One small interface for any calendar.** `app/calendars/base.py` has `Entry`, an event in the
  form both sides can hold, and `Provider`: read the changes, create, update, delete. Only
  `app/calendars/google.py` knows Google's payloads. It uses `httpx` and no Google library, as
  the chat adapters do.
- **Google is built; Apple is not.** Both did not fit one reviewable change. An Apple provider
  needs CalDAV discovery, `sync-collection`, reading and writing iCalendar with a changed
  occurrence as a `RECURRENCE-ID`, a form for the app-specific password, and tests against a local
  CalDAV server. Nothing in the sync itself has to change for it, and `calendar_connections`
  already accepts `apple`.
- **Reading is a poll every two minutes with the sync token, not a push channel.** A channel is a
  public webhook, a secret to check on it, and a renewal job, none of which could be tried
  without credentials; a poll that is wrong fails where it can be seen. One connection is 720
  small reads a day. The first read asks for what ends after now (`timeMin`), with
  `showDeleted=true` and `singleEvents=false` as on every later read.
- **Writing is a comparison every 15 seconds, not a hook in the services.** The worker compares
  each event as it would be sent (`entry_of`) with `calendar_links.synced`, the event as both
  sides last agreed on it, and sends what differs: a `PATCH` of the changed fields only, a
  create, or a delete when the event is cancelled or gone. Because it looks at rows and not at
  calls, an undo, a dashboard edit and anything written later are covered without knowing about
  the sync. Every write carries `sendUpdates=none`: nobody is emailed.
- **A round never holds the household's lock while it talks to Google.** It reads; applies under
  the lock that a chat turn takes; sends; notes what was sent.
- **An echo is told from a change twice over.** A change whose `etag` is the one our own write
  returned is skipped. Failing that, a change equal to `synced` changes nothing.
- **Three things stop a second copy.** An event this app creates carries its household id as a
  private property, so if the answer to the create is lost the event is known again on the next
  read, and a provable second copy is deleted. An event that arrives with the same title and
  start as one already here is linked to it. And one calendar can be shared with a household
  once: a second adult picking it is refused.
- **Both sides changed: details are merged, and for the same detail the calendar wins.** What
  changed there since `synced` is applied here first, field by field, so a time moved on the
  phone and a place changed in chat both survive.
- **A change that arrives is a logged, undoable action** with `source = 'calendar'`, in the name
  of the adult whose calendar it is, made through `services/calendar.py` like any other. It can
  be undone on the Activity page, which then removes it from the calendar too. "Undo" in chat
  leaves it alone: that word means what the person said or did here.
- **Repeats map onto what the household already has.** A series is its `RRULE` and skipped
  dates. Google keeps a changed or deleted occurrence as an event of its own; here that is a
  skipped date and, if it was changed, a one-off event, exactly as when one date is changed in
  chat. Such a date is remembered in `calendar_links.instances` and never sent back as an
  `EXDATE`, because Google already holds it. An event with `RDATE`, more than one rule or a
  repeat shorter than hourly is left out.
- **Events can last all day** (`events.all_day`). They have no reminders; the morning brief names
  them on the day and the day before, and the feed writes them as dates.
- **The assistant sends the reminders.** An event this app creates is told to use none of
  Google's. An event made in Google keeps whatever Google does with it and also gets the
  assistant's.
- **Who is reminded about an event from a calendar.** Family members named in its title take
  part ("Tobi swimming"), so it goes to the household. With nobody named it is its owner's.
- **Not brought in:** birthdays, focus time, out of office and working location, which are
  Google's own kinds of entry; and anything already over.
- **The sign-in is a secret.** The refresh token is stored encrypted (Fernet, from the
  `cryptography` package) with `CALENDAR_SECRET_KEY`, which only the environment holds; access
  tokens are kept in memory. The scopes are the two that do the job:
  `calendar.calendarlist.readonly` and `calendar.events`. The `state` that goes to Google and
  back is signed and names who set off. The code in the callback's address is masked in the
  access log, and the household export leaves the secret out.
- **When it fails.** A fault or a rate limit waits 30 seconds, then twice as long each time, up to
  an hour. A sign-in that no longer works stops the connection and tells its owner once, with
  how to connect again.

## Consequences

- **None of this has met Google.** The tests replay responses written from the documented
  shapes. These assumptions want checking first: that a first read with `timeMin` returns a
  series that began earlier; that a `null` in a `PATCH` clears a field, and that this is how an
  event changes between all day and timed; that an `EXDATE` written through the API sits beside
  Google's own changed occurrences; that the `etag` a write returns is the one the next list
  carries; and that the token's `scope` names both scopes when both boxes were ticked.
- **Google's own setup limits who can connect.** While the OAuth consent screen is in "Testing",
  only listed test users can connect and a refresh token stops working after 7 days. Both
  scopes are ones Google reviews before an app is published to everyone
  ([operations.md](../operations.md#google-calendar)).
- A plan made in chat is on the calendar within about 15 seconds; a change made on the phone is
  here within about two minutes.
- Polling does not scale to many households on Google's daily quota. Push channels are the next
  step then, and so is a way to find changed events without comparing every link every 15
  seconds.
- A date deleted from a series on the phone and then put back is not noticed, and undoing such a
  skip here does not put the date back there.
- A series made in another time zone repeats at its household-time hour, so it is an hour out
  for the weeks when the two zones' clocks differ.
- An event made in Google can be reminded twice: by Google and by the assistant.
- An event the person does not organise may refuse a change from here. That change is given
  up and logged, and the two copies differ until the event next changes there; the calendar's
  version is then the one kept. A new event the
  calendar refuses is tried again after a wait, and does not hold the others back.
- If both adults connect different calendars and also subscribe to each other's in Google, each
  sees an event twice there.
- Disconnecting deletes nothing on either side. Connecting again and picking the same calendar
  carries on with what was linked.
- An all-day event cannot yet be made in chat or on the Calendar page; one arrives only from a
  connected calendar.
- Only events are copied. A reminder with no event ("remind me to call the plumber on Friday")
  stays with the assistant and is not on the calendar.
- On connecting, everything still to come on the chosen calendar comes in, and everything still
  to come here goes onto it. The page says so before the person picks.
- One new dependency, `cryptography`: the standard library has no cipher.
