# 0041 Apple Calendar is CalDAV, signed in with an app-specific password

## Context

[ADR 0038](0038-a-connected-calendar-is-a-second-copy-kept-in-step.md) built two-way sync for
Google Calendar behind two small types, `Entry` and `Provider`, and left Apple for a second
change. Half of a couple is often on an iPhone, so the half of their plans that is made there
never reached the assistant.

What Apple offers, as read in October 2026:

- **iCloud Calendar speaks CalDAV** (RFC 4791) at `https://caldav.icloud.com`. Apple publishes no
  page about it. What is known comes from the projects that use it: Home Assistant, vdirsyncer
  ("regularly tested against iCloud") and DAVx5 all name that address.
- **The sign-in is the Apple Account's email and an app-specific password.** Apple's page
  ([support.apple.com/102654](https://support.apple.com/en-us/102654)) gives the steps: at
  account.apple.com, under Sign-In and Security, App-Specific Passwords, Generate an app-specific
  password. The account has to have two-factor authentication. There can be 25 at once, each can
  be removed by itself, and all of them are cancelled when the main password changes. The same
  page mentions that "supported third-party apps" can be authorised with the Apple Account
  instead, and says nothing about how; nothing is published that a self-hosted app could use.
- **There is no push to a server.** A CalDAV client asks what changed.

**No Apple account existed when this was built, so nothing here has been run against iCloud.**
A few requests were made to it without an account, to see what it says it supports and what a
refused sign-in looks like: `401` with an empty body.

## Decision

**Apple is a second provider behind the same two types, and the sync is the same sync.** Only
`app/calendars/apple.py` knows CalDAV and iCalendar. An adult still connects one calendar, and
everything ADR 0038 says about the hub, echoes, second copies, merging, undo and reminders holds
unchanged.

- **The sign-in is a form, not a trip.** On the Calendar page the person types the email they
  sign in to Apple with and an app-specific password; the page gives Apple's two steps and a
  link to account.apple.com. The pair is tried at once against iCloud and kept only if Apple
  accepts it, encrypted with the same `CALENDAR_SECRET_KEY` as a Google sign-in. It is in a form
  body, never in an address; it is not a logged action, not in any log line, and not in the
  export. A wrong one is asked of Apple once, because Apple counts wrong passwords.
- **Apple needs no setup by whoever runs the installation.** There is no client to register:
  `CALENDAR_SECRET_KEY` alone switches it on. `APPLE_CALDAV_URL` exists so the tests, and anyone
  trying it out, can point it at another CalDAV server; it defaults to iCloud.
- **The password is sent only to Apple.** iCloud answers from a host of its own for each account
  (`pNN-caldav.icloud.com`) and says which in its answers. An address in an answer is followed
  only if it is the configured server or a host beside it, over TLS.
- **The calendars offered** are the ones in the account's calendar home that hold events and
  that the person may write to. Reminders, the inbox and calendars someone else only lets them
  look at are left out.
- **Reading is RFC 6578's `sync-collection`**, every two minutes like Google: it lists the files
  that changed and the ones that went, with a token for next time, and the changed files are
  then fetched with `calendar-multiget`, 50 at a time. With no token it lists every file; the
  whole calendar is fetched once and what is already over is dropped. A list that comes in
  parts (`507`) is followed. A file that is listed but not handed over is asked for by itself,
  so one odd file cannot hold every other change back.
- **A token iCloud no longer knows means read everything again.** RFC 6578 says `403` or `409`
  with `valid-sync-token`. What iCloud answers is not published, so any refusal of a request
  that carried a token is taken that way; the read without one then says what is really wrong.
- **CalDAV says "gone" only by leaving something out, so the sync now tells a provider what it
  follows.** This is the one change to the shared sync: `Provider.changes` takes `followed`, the
  ids there is a link for. Google ignores it. Apple uses it three ways: after reading everything
  again, a followed file that is not listed was deleted while the place was lost; a file that
  went takes the changed dates in it with it; and a changed date that no longer has an event of
  its own was deleted or put back.
- **One file is one event, and a date changed by itself is one more.** CalDAV keeps a series and
  its changed dates in one file: the series, and a `VEVENT` with a `RECURRENCE-ID` for each such
  date. The file is one `Remote` whose id is its address. Each changed date is another, with
  the address, `#` and the date as its id, and it becomes a one-off plan here, as it does from
  Google and as it does when one date is changed in chat.
- **The series itself says which dates it skips.** A deleted date is an `EXDATE` in the file. A
  date with a `VEVENT` of its own is read as skipped too. Writing, the dates the file holds as
  `VEVENT`s are not written as `EXDATE`s as well. So `calendar_links.instances`, which exists
  because Google keeps its changed dates apart from the series, stays empty for Apple, and a
  date put back on the phone comes back here, which it does not from Google.
- **A change is made to the file that is there, not by writing a new one.** The file is read,
  only the changed properties are replaced, and it is written back with `If-Match`, so a change
  made on the phone in between is not overwritten: the write is refused, and tried once more
  from a fresh read. What Apple put in the file stays: its alarms, its travel time, its place
  on a map.
- **Making an event twice is not possible.** A new event's UID, and so its file's name, is
  `houseagent-`, the household event's id, and eight characters made from the calendar's
  address. It is written with `If-None-Match: *`; if it is there already it is made to match.
  That UID is also the mark that says the event is ours, as the private property is on Google.
  Only the series carries the mark: a changed date of our own series has the same UID and must
  not be taken for a second copy and deleted. The calendar's address is part of the UID because
  one iCloud account is reported not to hold the same UID on two of its calendars.
- **An event with guests is read and never written.** iCloud sends invitations, changes and
  cancellations by itself when such an event is written or deleted (RFC 6638; iCloud says
  `calendar-auto-schedule`). Google has `sendUpdates=none`; CalDAV has no equivalent that covers
  every case. Nobody is emailed by this app, so a change made here to such an event is given up,
  as a change Google refuses is.
- **A new event carries no alarm**, and a zone's definition beside every time written with a
  zone's name, which RFC 5545 requires.
- **When it fails**, as ADR 0038: `401` stops the connection and tells its owner once, with the
  extra line that it needs a new app-specific password. The worker asks once more before it
  believes a `401`, because stopping costs the owner a trip to Apple. `429` and `5xx` wait, a
  deleted calendar stops it.
- **Disconnecting cannot tell Apple.** The page says where to remove the password.

**One new dependency, `icalendar`, and one for the tests, `radicale`.**

- `icalendar` (7.3, pure Python, needs only `python-dateutil` and `tzdata`) reads and writes
  iCalendar. Writing a change into Apple's own file without losing what is in it needs a real
  parser that keeps what it does not understand; it also resolves Windows zone names and writes
  the zone definitions. A hand-written one would be the part most likely to be wrong.
- The `caldav` library was looked at and not used. It brings eight packages, among them a second
  HTTP client and `lxml`, for the six requests this needs, which `httpx` already makes. Its own
  notes on iCloud are commented out and marked as untested for years, and the released version
  sends `Depth: 1` on `sync-collection`, where RFC 6578 defines only `0`. Its source and tests
  were read for what iCloud is known to do; three of the fixtures are its captured answers.
- `radicale` is a CalDAV server in Python. The tests start one for the session.

## Consequences

- **None of this has met iCloud.** The tests run the real client against Radicale, a real CalDAV
  server, through the worker job, and replay iCloud's discovery answers as python-caldav's tests
  recorded them. These want checking first with a real account: that a `PUT` is answered with
  an `ETag`, or that asking for it afterwards works; what a forgotten token is answered with;
  that iCloud takes the zone definition this writes, and a UID and a file name that are not a
  bare UUID; that `calendar-multiget` hands over every file `sync-collection` lists; that the
  calendars offered are the right ones, since iCloud's answer to the write-permission question
  was not found published; that an event written here gets no alert of the phone's own; and how
  much asking iCloud allows, for which people report `503` and Apple publishes no number.
- A plan made in chat is on the phone within about 15 seconds; a change made on the phone is
  here within about two minutes.
- The first read fetches the whole calendar, however old, to find what is still to come. More
  than 10,000 files is refused.
- The app holds a password that is good for more than the one calendar picked: every calendar
  in the account, and, since Apple's page speaks of apps that use iCloud Mail, Calendar and
  Contacts, most likely those as well. Nothing here asks for anything but calendars. It is
  encrypted with a key only the environment holds, and removing it at Apple ends it.
- When the person changes their Apple password, Apple cancels every app-specific password. The
  connection stops and its owner is told once.
- A change made here to an event with guests is not sent, and the two copies differ until it
  next changes on the phone.
- When the time of a series changes here, its changed dates keep their own times and are told to
  name the same dates at the new time. If the series moves to another day of the week they no
  longer belong to any date of it; what a phone shows then is not known.
- An event made on the phone can be reminded twice, by the phone and by the assistant, as with
  Google.
- Two adults with separate Apple accounts who both pick a calendar they share see it under two
  addresses, so the "one calendar is shared once" check does not catch it. The UID mark does:
  the second connection takes each event the first made for its own copy, and nothing is made
  twice.
- A calendar that is not iCloud's can be connected by pointing `APPLE_CALDAV_URL` at it. That
  is how the tests work and it is not offered to a household: one address serves everyone.
