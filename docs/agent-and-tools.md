# Agent and tools

## Runtime

`AgentRuntime` (`app/agent/base.py`) has one method, `handle(envelope, ctx) -> AgentResult`.
`LoopRuntime` (`app/agent/loop.py`) is the default, and the one in use. `LettaRuntime` is an
optional second implementation ([below](#the-letta-runtime-optional)). The loop:

1. System prompt = the static prompt + the household brief, + the onboarding section while the
   household is being set up.
2. Messages = the thread's last 20 messages from the past 48 hours (member messages as
   `"{name}: {text}"`, agent messages as assistant turns, a bare reaction as `ACK`), then the
   current turn: its text and up to four photos.
3. Loop up to `LLM_MAX_TOOL_ITERATIONS`: call the model; if it asks for tools, run each in order
   and return one tool message per call; otherwise stop.
4. Final text: exactly `ACK` means react only, exactly `NOOP` means say nothing, anything else is
   the reply. An empty answer counts as `ACK` if a tool call succeeded in the turn, else `NOOP`.
   Hitting the iteration cap replies "I got a bit lost there, can you say that again?"

The static prompt is `STATIC_PROMPT` in `app/agent/prompt.py`; that file is the source of truth.
The brief (`build_brief`) lists the time in household time, who is speaking, family, locations,
facts, the shopping list, low or out items, items expiring within 3 days and the events of the
next 7 days, each list capped at 25 entries with "+N more". Its last line is the speaker's most
recent change that can still be undone, as the tool reported it: thread history holds what was
said, not what was recorded ([ADR 0031](adr/0031-rules-the-model-kept-breaking.md)).

A turn's clock is the time its message arrived (`Ctx.now`), not the time the worker got to it.
"In two hours" and "tomorrow" count from when it was said. See
[ADR 0013](adr/0013-explicit-clocks.md).

## The Letta runtime (optional)

`AGENT_RUNTIME=letta` swaps the loop for `LettaRuntime` (`app/agent/letta_runtime.py`), which
hands the conversation and the model calls to a [Letta](https://docs.letta.com) server. **It is
off by default and has not been promoted**: on the eval suite it did not beat the loop
([evals.md](evals.md#the-letta-comparison)). It needs the `letta` extra (`letta-client`),
`LETTA_BASE_URL`, `INTERNAL_TOOL_TOKEN`, and usually `LETTA_MODEL`, the model's handle as that
Letta server names it.

- **One Letta agent per household**, created on its first turn; its id is kept in
  `households.letta_agent_id`. Letta's own memory tools are not attached.
- **Postgres stays the source of truth.** Before every turn the runtime rewrites the agent's two
  memory blocks: `persona` (the static prompt) and `household` (the brief, and the setup section
  while setup is open). Thread history is not sent; Letta keeps its own, one for the whole
  household, DMs and group together.
- **Tools** are the same twelve, registered with Letta as functions whose whole body posts the
  arguments to `POST /internal/tools/{name}` with `Authorization: Bearer INTERNAL_TOOL_TOKEN`.
  The household id, the address to call and the token come from the agent's tool environment,
  which is also rewritten before every turn.
- **The bridge** (`app/agent/internal.py`) answers `{"result": str, "is_error": bool}`. It runs the
  call on the connection of that household's turn in flight, as that turn's member and message,
  so the writes, the undo log and the outbox are exactly the loop's. A call with a wrong token
  gets 401; with no turn in flight for that household, 409; with no token configured, 404.
- **Which process answers.** The bridge runs in whichever process is running the turn: the
  worker for chat (it listens on the port of `WORKER_INTERNAL_URL`, 8001 by default) and the api
  for Practice chat (`INTERNAL_BASE_URL`, by default `PUBLIC_BASE_URL`). `/internal` must be
  reachable from the Letta server and from nowhere else; the Helm chart keeps it off the
  ingress ([ADR 0029](adr/0029-the-letta-tool-bridge.md)).
- **Photos** go to Letta as base64 image parts of the user message.
- The final answer is read as in the loop: `ACK`, `NOOP`, or a reply. A Letta failure fails the
  turn, and the member gets the usual "Sorry, that didn't go through".

What is different under Letta: a Practice chat run that is not saved rolls the database back but
Letta still remembers the exchange; the model's own instructions are Letta's system prompt with ours in a
memory block; and message text is stored on the Letta server as well as in Postgres.

Letta's documentation now describes a different product (a harness with a WebSocket App Server).
This runtime uses the REST API that `letta-client` wraps, which the documentation lists for
Letta Cloud. Self-hosted, that API is the server image up to `letta/letta:0.16.8`; later tags of
the same image are the new harness and do not serve it.

## Photos

A turn's photos are loaded through `MediaStore` and sent to the model as images after the text.
What the model does with them is in the static prompt: a receipt becomes `log_inventory` with
`restocked` and source `receipt`; a fridge, freezer or cupboard photo becomes `adjusted` with
source `photo` and that location, and nothing is ever marked finished for being absent from a
photo. The usual rules then apply in code, so a receipt ticks bought items off the shopping list,
and a line such as "BASMATI RICE 5KG" is not made a second item beside the household's rice
([below](#resolution-appagentresolvepy)).

When a photo cannot be shown to the model the turn still runs, with a note in place of the image:

| Situation | Note in the turn |
| --- | --- |
| `LLM_SUPPORTS_IMAGES=false` | `[photo received; this model can't read photos]` |
| No media backend, or the download, upload or read failed | `[photo received; it could not be loaded]` |
| More than four photos in one batch | `[2 more photos not read: at most 4 per message]` |

Earlier photos appear in thread history as `[photo]` lines only; the image is not sent again.

## Onboarding

A new household starts with `onboarding_state.step = "family"`. While a step is open the system
prompt ends with the spec's onboarding section (current step and what remains), one line saying
what this step asks and how to record it, and the model is offered `onboarding_advance`.

| Step | The agent asks | Recorded with |
| --- | --- | --- |
| `family` | Who lives here, including the kids? | `add_family_member` |
| `routines` | Regular things: nursery, classes, clubs? | `schedule_event` with a repeat rule |
| `shops` | Where do you usually shop? | `remember` keys `main_supermarket` and `shops` |
| `staples` | What do you always need in the house? | `remember` key `staples` |
| `tour` | Photos of the fridge, freezer and cupboard | `log_inventory`, `adjusted`, source `photo` |
| `rhythm` | Morning brief at 07:30, quiet 21:30 to 07:00, OK? | `remember` keys `morning_brief`, `quiet_hours` |
| `presence` | Nothing | Code sends each connected adult a private message with their Shortcut link |

The first question is not a model call: when an adult connects while `family` is open, the
"you're connected" message asks it. Steps can be skipped or answered out of order, and everything
they record can be said later in ordinary conversation. After the last step the section and the
tool disappear. See [ADR 0017](adr/0017-onboarding-state-and-messages-written-by-code.md).

The seventh step is done by code. When `onboarding_advance` leaves `presence` as the only step
open, it sends each connected adult one short message offering the shopping list on arrival at a
shop, marks the step done, and tells the model in a `NOTE:` line who was sent it.
`onboarding_advance(presence, skipped=true)` before that sends nothing
([ADR 0025](adr/0025-the-presence-step-is-done-by-code.md)). The offer holds no link. An adult
who sends the single word `shops` is answered by the pipeline, not the model, with their personal
link, as `dashboard` is answered with a login link
([ADR 0033](adr/0033-the-presence-link-opens-a-page-and-is-sent-on-request.md)).

## Messages the assistant starts

Three messages are written by code and answered through the agent, which finds the question in
the thread history like any other assistant message:

| Sent | Text | A reply |
| --- | --- | --- |
| On arriving at a shop | "You're at Tesco Extra. On the list: ..." | "Got everything" ticks off what was asked for, not the "(probably)" entries |
| On leaving home | "You're out. The list has 9 items, want it?" | "yes" reads the list with `get_shopping_list` |
| At 17:30 | "Probably running low: milk, bread. Add to the list?" | "yes", or "just the milk", adds with `update_shopping_list`; the guessed entry becomes a real one |

Token usage, tool calls and latency for each turn are stored on the batch's last message in
`messages.meta` (`usage`, `turn`), and shown to an admin on the dashboard Activity page, under
"Technical details".

## Tools

`REGISTRY` in `app/agent/tools/__init__.py` holds the twelve tools. Eleven are always offered;
`onboarding_advance` only while a household is being set up.

| Tool | Writes | Behaviour |
| --- | --- | --- |
| `log_inventory` | events, stock, list | Per change: resolve location and item, append the event, apply the stock transition, run the side-effect rules. Every `finished` and `low` line says what happened to the shopping list |
| `query_inventory` | none | Filter by item, location, status, expiry; never creates an item |
| `update_shopping_list` | list, events | `add`, `bought` (logs `restocked`, source `shopping`), `remove` (dismissed), `bought_all` |
| `get_shopping_list` | none | Explicit, finished and low entries first, then predicted ones marked "(probably)"; the optional store filter keeps entries naming no shop and those whose shop reads like the one asked for |
| `schedule_event` | events, reminders | Resolves participants, creates the event and its reminders; a repeat is an RFC 5545 rule |
| `modify_event` | events, reminders | Finds the event from how it was described; moves, edits or cancels it; scope `this` on a series changes one date |
| `list_upcoming` | none | Events in the next `days`, repeats expanded, plus standalone reminders; optional person filter |
| `set_reminder` | reminders | One-off at `fire_at`, or repeating by `rrule`; for me, the household or a named member |
| `remember` | facts; some settings | Upserts a fact for the household or one member; no value forgets it. The keys `staples`, `shops`, `main_supermarket`, `morning_brief` and `quiet_hours` are settings ([ADR 0016](adr/0016-settings-said-in-chat-go-through-remember.md)). Quiet hours are set for whoever `about` names (`us` is every adult), and for the speaker alone when nobody is named |
| `add_family_member` | members, an invite | Adds a child or an adult; a name already there is not added twice. For an adult who has not connected, an invite is sent to the person asking, to pass on |
| `undo_last` | inverse of the last actions | The caller's own actions from the past 24 hours, newest first, `n` up to 5 |
| `onboarding_advance` | onboarding state | Marks a setup step done or skipped and names the next one |

Result lines are prefixed so the model can relay or act on them:

```text
OK: egg finished (fridge)
NEW: Scotch bonnet
NOTE: egg added to shopping list
OK: bread finished (store)
NOTE: bread not added to shopping list: it is not a staple
AMBIGUOUS: 'pepper' could be Bell pepper (fridge), Black pepper (store)
ERROR: 'basmati rice' not recorded. The household already has rice (store). Decide which this is without asking: the same thing, then log it again under that name; a different product, then log it again with new_item true
ERROR: the shopping list is empty
OK: GP for Ada on Wed 7 Oct at 10:30, Hurley Clinic
NOTE: reminders at Tue 6 Oct 10:30, Wed 7 Oct 09:30
NEW: Ada (adult)
NOTE: an invite for Ada was sent to this person in a separate message; they pass it on and Ada connects by opening it
OK: quiet hours for Ada: 20:00 to 07:00
OK: staples done. Next step: tour
```

`household_id` and `member_id` come from `Ctx`, never from the model. No tool can name a recipient:
the invite for a new adult goes to whoever asked, and its code is written by code, not by the model.

`run_tool` validates arguments with the tool's Pydantic model and runs the tool in its own
savepoint. Validation and domain errors come back as `ERROR:` tool messages so the model can
recover; an unexpected exception is logged and reported to the model the same way.

## Calendar rules (`app/services/calendar.py`)

**Times.** The tools ask the model for household local time without an offset, for example
`2026-10-28T10:30`. A naive time is read in the household time zone; a time that does carry an
offset is honoured. A one-off event or reminder in the past is an `ERROR:` that states the current
time, so the model can correct a wrong date.

**Reminders for an event.** `remind_before_minutes` defaults to a day and an hour before.

- A reminder whose time has already passed is not created.
- Who gets it: the household if a child or more than one adult takes part, otherwise that adult.
- Wording: `{title} {today|tomorrow|on Wed 7 Oct} at {HH:MM}{, location}`. "Today" and "tomorrow"
  are relative to when it will be read, which is the end of quiet hours if it is held until then.
- A day-before reminder that lands in the recipient's quiet hours is not created. The event
  appears in that morning's brief under "Tomorrow" instead.
- A shorter reminder that lands in quiet hours is held until they end, unless that would be after
  the event starts. Then it is marked urgent and goes out on time: the hour-before for a 06:30
  event arrives at 05:30.
- Moving or editing an event updates its scheduled reminders in place where the time is unchanged,
  deletes the ones that no longer apply and adds the new ones. Cancelling marks them `cancelled`.

**Recurring events.** The rule is validated and expanded with `python-dateutil` from a start in
household time. Frequencies shorter than hourly are refused. An `UNTIL` without a `Z` is read as
household time and stored in UTC. `modify_event` with scope `this` skips one date (the next
occurrence, or the one on `occurrence`) and, unless it is a cancellation, adds a one-off event with
the change; scope `all` changes or cancels the series.

**Standalone reminders.** `set_reminder` needs `fire_at` or an `rrule`. A repeat needs a time of
day, from `fire_at` or `BYHOUR`, and an end is given with `UNTIL`, not `COUNT`. A reminder the
user sets for a time inside their own quiet hours, or with `urgent`, is sent at that time.

**Saying it twice.** Scheduling the same title at the same time again, or the same reminder text
at the same time, changes nothing and says so. Models sometimes repeat a call from the history.

**Finding the event.** `modify_event` ranks active events whose next occurrence is within 60 days
by trigram similarity between the description and the title plus participant names, and takes the
best if it scores at least 0.3, the soonest on a tie. No match is an `ERROR:` listing what is coming up.

## Resolution (`app/agent/resolve.py`)

1. **Normalise**: trim, drop leading articles and quantities ("a dozen", "2x"), singularise the
   last word ("eggs" to "egg"). Words such as "oats", "noodles" and "peas" stay plural, and words
   ending in `ss`, `us`, `is`, `as` are left alone.
2. **Exact** match on the canonical name or an alias, ignoring case: hit.
3. **Fuzzy**: trigram `similarity()` against names and aliases. The best candidate must reach 0.55.
   If no other candidate is within 0.15 of it, it is a hit and the spelling is saved as an alias.
4. **Ambiguous**: another candidate within 0.15 of the best. Nothing is recorded for that entry;
   the options are returned with their usual locations.
5. **New**: no candidate. The item is created with the normalised name in its original casing,
   in the stated location or `store`.

The 0.15 comparison includes candidates below 0.55, so "pepper" with Bell pepper (0.58) and Black
pepper (0.54) on file asks which one instead of silently picking Bell pepper.

**A name that ends with one the household has.** `log_inventory` does not create "basmati rice"
beside "rice", or "coconut milk" beside "milk", on its own word. When a name matches nothing and
its last words are an item's name or alias, nothing is recorded for that change and the result
names the item. The model then logs it under the household's name, or again with `new_item: true`
if it is a different product. Code can see that the names are related; only the model can say
whether semi skimmed milk is the milk. `update_shopping_list` and the `staples` setting do not ask.

Locations resolve the same way. The seed locations carry aliases (`deep freezer`, `pantry`,
`cupboard`, `larder`, `refrigerator`), and an unknown location name creates a custom location.

People: "me" is the speaker, "us" every adult, "the kids" every child; anything else is matched
on member names the same way. A name that matches nobody does not block the event: it is created
without that person and the result carries a `NOTE:`.

## Undo (`app/agent/actions.py`)

Every tool call or dashboard action that writes runs inside `record(ctx, tool, args)`. Its
`Recorder` captures each row's state the first time the action touches it. That becomes the
action's inverse in `agent_actions.inverse`:

```json
[
  {"op": "restore_rows", "table": "stock", "rows": [{"item_id": "...", "location_id": "...", "qty_estimate": 6, "status": "in_stock", "expires_on": null, "last_event_at": "..."}]},
  {"op": "delete_rows", "table": "shopping_list_items", "ids": ["..."]}
]
```

Rows that existed are restored in full; rows the action created are deleted. Undoable tables are
`stock`, `shopping_list_items`, `items` (the staple flag and dashboard edits), `events`,
`reminders`, `household_facts`, `places`, `members` and `households`. For the last two only the
columns an action may change are put back (name, role, preferred channel and quiet hours; brief
time and onboarding state), never an invite, a token or the session version, so an undo cannot
revive a revoked invite or a logged-out session. Undoing "add Tobi" removes him; once a member
has connected, or anything else depends on them, that undo is refused. Undoing a new event deletes it and its reminders; undoing a move, an edit, a skipped
date or a cancellation puts the event and every reminder row back as they were. Undo:

- applies the newest non-undone action first and sets `undone_at`;
- is refused with `ERROR:` if a later action by anyone touched the same rows;
- appends an `adjusted` event with source `undo` per reverted stock row; no history is deleted;
- from chat covers the caller's own actions within 24 hours; from the dashboard Activity page any
  adult can undo any single action.

Two things are done for the model, because live models got them wrong
([ADR 0031](adr/0031-rules-the-model-kept-breaking.md)):

- The call an undo just reverted is not run again in the same turn. The identical call answers
  `OK: not done again: that is exactly what was just undone`.
- `low` logged right after `finished` takes the `finished` back first ("not gone, just low"),
  when that one change was the whole earlier action, it is under 24 hours old and nothing has
  touched the item since. The result says so in a `NOTE:`, and the quantity is the one from
  before the mistake, not zero.

Not undone: item and location creation, learned aliases, merging duplicate items, creating or
revoking an invite, and an invite that was already sent (the code dies with the member).
