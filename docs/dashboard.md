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
| Today | `/dashboard` | Shopping list count, low and out items, items expiring within 3 days | Quick-add to the list |
| Shopping list | `/dashboard/shopping` | Active list, then a "probably needed" section | Add, tick off, remove, set the shop |
| Inventory | `/dashboard/inventory` | Stock by location, filter by status; per-item history | Set a count, mark finished, edit an item (aliases, staple, threshold, usual place), merge a duplicate |
| Activity | `/dashboard/activity` | The last 200 turns and dashboard actions: message, tool calls and results, tokens, latency, send status | Undo an action, retry a failed send |
| Playground | `/dashboard/playground` | A chat with the agent in the browser | Dry run by default; tick "Apply for real" to keep the result |

Today will also show events and reminders once the calendar exists. Calendar, Family, Channels,
Settings and System pages are not built yet.

## How writes work

Pages never write SQL. A write calls the same `app/services/` function the agent tool uses, inside
`record(...)` with `source = 'dashboard'`, so stock rules, undo and the Activity log behave exactly
as they do from chat. Ticking "Bought" in the browser logs a restock, just as "got the eggs" does.

The Playground calls `simulate_turn()`, the same service the eval suite uses. A dry run executes
the whole turn in a transaction and rolls it back, so nothing is saved and each dry-run message
starts without memory of the last one.

Merging duplicates reassigns events, stock, list entries and aliases to the kept item in one
transaction and cannot be undone.
