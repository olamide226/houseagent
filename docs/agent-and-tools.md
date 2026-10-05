# Agent and tools

## Runtime

`AgentRuntime` (`app/agent/base.py`) has one method, `handle(envelope, ctx) -> AgentResult`.
`LoopRuntime` (`app/agent/loop.py`) is the only implementation so far:

1. System prompt = the static prompt + the household brief.
2. Messages = the thread's last 20 messages from the past 48 hours (member messages as
   `"{name}: {text}"`, agent messages as assistant turns, a bare reaction as `ACK`), then the current turn.
3. Loop up to `LLM_MAX_TOOL_ITERATIONS`: call the model; if it asks for tools, run each in order
   and return one tool message per call; otherwise stop.
4. Final text: exactly `ACK` means react only, exactly `NOOP` means say nothing, anything else is
   the reply. An empty answer counts as `ACK` if a tool call succeeded in the turn, else `NOOP`.
   Hitting the iteration cap replies "I got a bit lost there, can you say that again?"

The static prompt is `STATIC_PROMPT` in `app/agent/prompt.py`; that file is the source of truth.
The brief (`build_brief`) lists the time in household time, who is speaking, family, locations,
facts, the shopping list, low or out items and items expiring within 3 days, each list capped at
25 entries with "+N more".

Token usage, tool calls and latency for each turn are stored on the batch's last message in
`messages.meta` (`usage`, `turn`), and shown on the dashboard Activity page.

## Tools

`REGISTRY` in `app/agent/tools/__init__.py` holds the tools the model is offered. Five are
implemented. The contract (argument models and descriptions) for the other seven lives in
`calendar.py`, `memory.py`, `family.py` and `onboarding.py` and is registered by the milestone
that implements each.

| Tool | Writes | Behaviour |
| --- | --- | --- |
| `log_inventory` | events, stock, list | Per change: resolve location and item, append the event, apply the stock transition, run the side-effect rules |
| `query_inventory` | none | Filter by item, location, status, expiry; never creates an item |
| `update_shopping_list` | list, events | `add`, `bought` (logs `restocked`, source `shopping`), `remove` (dismissed), `bought_all` |
| `get_shopping_list` | none | Explicit, finished and low entries first, then predicted ones marked "(probably)"; optional store filter |
| `undo_last` | inverse of the last actions | The caller's own actions from the past 24 hours, newest first, `n` up to 5 |

Result lines are prefixed so the model can relay or act on them:

```text
OK: egg finished (fridge)
NEW: Scotch bonnet
NOTE: egg added to shopping list
AMBIGUOUS: 'pepper' could be Bell pepper (fridge), Black pepper (store)
ERROR: the shopping list is empty
```

`household_id` and `member_id` come from `Ctx`, never from the model. No tool can name a recipient.

`run_tool` validates arguments with the tool's Pydantic model and runs the tool in its own
savepoint. Validation and domain errors come back as `ERROR:` tool messages so the model can
recover; an unexpected exception is logged and reported to the model the same way.

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

Locations resolve the same way. The seed locations carry aliases (`deep freezer`, `pantry`,
`cupboard`, `larder`, `refrigerator`), and an unknown location name creates a custom location.

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
`stock`, `shopping_list_items` and `items` (the staple flag and dashboard edits). Undo:

- applies the newest non-undone action first and sets `undone_at`;
- is refused with `ERROR:` if a later action by anyone touched the same rows;
- appends an `adjusted` event with source `undo` per reverted stock row; no history is deleted;
- from chat covers the caller's own actions within 24 hours; from the dashboard Activity page any
  adult can undo any single action.

Not undone: item and location creation, learned aliases, and merging duplicate items.
