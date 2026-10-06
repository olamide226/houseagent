# Dashboard

A phone-first web dashboard at `/dashboard`, rendered by the api process with Jinja2 and HTMX.
It is for setup, looking things over and fixing mistakes; daily use happens in chat.

## First run

Open `/setup?token=<SETUP_TOKEN>` while no household exists. The page asks for the household name,
time zone and the admin's name, creates the household with locations `fridge`, `freezer` and
`store`, and shows the admin's invite once: a Telegram deep link, a QR code of it, and the raw
code. After that `/setup` returns 404.

## Login

No passwords. A connected adult sends `dashboard` to the bot. The pipeline answers that exact
keyword itself (no model call) with a DM containing `/login/{token}`: 32 random bytes, stored
hashed, valid 10 minutes, single use, 5 per member per hour. Opening it sets a signed, HttpOnly,
SameSite=Lax cookie for 30 days (Secure when `PUBLIC_BASE_URL` is https) carrying the member id and
`members.session_version`. `POST /logout?everywhere=1` bumps that version and ends every session.
Children never get logins.

Every POST carries a CSRF token derived from `SESSION_SECRET` and the session, in the
`X-CSRF-Token` header (set once on `<body>` via `hx-headers`) or a `csrf` form field. A mismatch
returns 403.

## Pages

| Page | Path | Shows | Actions |
| --- | --- | --- | --- |
| Today | `/dashboard` | Shopping list count, today's and tomorrow's events and reminders, low and out items, items expiring within 3 days | Quick-add to the list |
| Shopping list | `/dashboard/shopping` | Active list, then a "probably needed" section | Add, tick off, remove, set the shop |
| Inventory | `/dashboard/inventory` | Stock by location, filter by status; per-item history | Set a count, mark finished, edit an item (aliases, staple, threshold, usual place), merge a duplicate |
| Calendar | `/dashboard/calendar` | The next 30 days with repeats expanded, the repeating series, standalone reminders | Add an event (once, daily, weekly or monthly), edit title, time and place, cancel, skip one date of a series, cancel a reminder, get the subscribe link |
| Family | `/dashboard/family` | Everyone in the household, each adult's connected channels, open invites | Add an adult or a child, make a new invite (link, QR code and code, shown once), revoke an invite, choose which connected channel an adult is messaged on |
| Channels | `/dashboard/channels` | Each channel: whether it is set up, when it last heard from and sent to the family, sends failed in the last day. Each chat: whose it is, the main family chat, and for WhatsApp whether an ordinary message or only the template will be delivered | Create a WhatsApp group, forget one that was never confirmed, show a group's invite link and QR code, choose the main family chat |
| Activity | `/dashboard/activity` | The last 200 turns and dashboard actions: message, tool calls and results, tokens, latency, send status | Undo an action, retry a failed send |
| Playground | `/dashboard/playground` | A chat with the agent in the browser | Dry run by default; tick "Apply for real" to keep the result |
| Settings | `/dashboard/settings` | The morning brief time, each adult's quiet hours, whether each adult has a presence link, places and their kinds, remembered facts | Change the brief time, change or clear quiet hours, make or replace a presence link, add a place or change its kind, add, change or forget a fact |

The System page is not built yet. Reminders are added in chat; the Calendar page lists
and cancels them. Staples are set on an item's page. The calendar subscribe link stays on the
Calendar page.

## Presence links and places

"Arriving at the shops" on Settings lists each adult with "has a link" or "no link yet". "Make
link" or "Replace link" creates a new personal link and shows it once with the phone steps; only
its hash is stored, so it cannot be shown again, and replacing it stops the old link working.
Any adult can make a link for any adult, as with invites. The steps for the phone are in
[presence.md](presence.md#phone-setup-once-per-place).

"Places" lists what the assistant knows by name: the shops said during setup, anything added
here, and any name a phone has sent. Only a place of kind `store` sends its list on arrival, and
only `home` counts for "out and about", so a place that appeared as `other` needs its kind set
here. Changing a kind is logged in Activity and can be undone; making a link is logged and cannot.

## Inviting the family

Adding an adult on the Family page creates the member and shows their invite once: a Telegram
deep link, a QR code of it, and the code itself, which can be sent to the bot on any connected
channel. Only the code's hash is stored. It works once per channel and for 7 days; "New invite"
replaces it and "Revoke" ends it. The same happens from chat: tell the assistant another adult
lives there and it sends you the invite to pass on. When they open it their chat is linked, it
becomes their preferred channel, and they can send `dashboard` to log in themselves. Children are
records only: no chat, no login, no quiet hours.

Quiet hours are on Settings rather than Family, next to the brief time they belong with. A fact
named `shops` or `main_supermarket` also becomes a shop in `places`. The keys `staples`,
`morning_brief` and `quiet_hours` are settings, not facts, and are refused in the facts form
([ADR 0016](adr/0016-settings-said-in-chat-go-through-remember.md)).

## Channels and the family group

What is addressed to the whole household (the morning brief, a reminder for a child's activity)
goes to the main family chat: the first group a connected member writes in, or the one chosen with
"Make main chat". With no group, each adult gets it by DM.

"Create group" asks WhatsApp for a group with the given name. WhatsApp creates it a moment later
and tells the api by webhook; until then the page shows it as "being created", and once it exists
it is the main family chat. If the confirmation never comes, "Forget" drops the request so it can
be made again. "Invite link" then asks WhatsApp for the group's link and shows it
with a QR code. The link is fetched each time and never stored. Each adult opens it to join, since
WhatsApp does not let a business add people to a group. Anyone holding the link can join, so pass
it on privately ([ADR 0021](adr/0021-whatsapp-groups-are-created-asynchronously.md)).

To use a group that already exists on Telegram, add the bot to it and write something there.

Moving someone's DMs to another channel is on the Family page: "Message here" next to a person
with more than one connected channel. Nothing else needs changing.

## Calendar feed

"New subscribe link" on the Calendar page creates a link of the form `/ics/{token}.ics` for Apple
or Google Calendar. The token is 32 random bytes and only its SHA-256 is stored, so the link is
shown once; making a new one stops the old one working. The feed is read-only: one `VEVENT` per
active event, `DTSTART` with the household `TZID`, the `RRULE` and exception dates passed through,
`UID` `{event_id}@household-agent`, and a 15-minute `Cache-Control`. It carries no `VTIMEZONE`
block ([ADR 0015](adr/0015-ics-feed-token-and-time-zones.md)).

## How writes work

Pages never write SQL. A write calls the same `app/services/` function the agent tool uses, inside
`record(...)` with `source = 'dashboard'`, so stock rules, undo and the Activity log behave exactly
as they do from chat. Ticking "Bought" in the browser logs a restock, just as "got the eggs" does.
Brief time, quiet hours, facts, added members and the preferred channel can be undone from
Activity. Creating or revoking an invite, creating a group and choosing the main family chat are
logged but not undoable, and a member who has connected can no longer be removed by undo.

The Playground calls `simulate_turn()`, the same service the eval suite uses. A dry run executes
the whole turn in a transaction and rolls it back, so nothing is saved and each dry-run message
starts without memory of the last one.

Merging duplicates reassigns events, stock, list entries and aliases to the kept item in one
transaction and cannot be undone.
