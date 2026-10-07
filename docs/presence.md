# Presence and predictions

Two things happen without anyone asking: the list for a shop arrives when you walk into it, and
the assistant asks about things that are probably running low. Both are optional. If nobody sets
up a phone, everything else works as before.

## Arriving at a shop

Each adult's iPhone runs a Shortcuts automation per place. When the phone arrives, the automation
calls that adult's personal link, and the assistant sends them the shopping list for that shop.
There is no app, no background location on the server, and no coordinates are stored: the server
only ever hears "Ola entered Tesco Extra".

### Getting your link

- **During setup.** When the last setup question is answered, each connected adult is sent a
  private message with their link and these steps. An adult who connects later gets theirs after
  the welcome.
- **On the dashboard.** Settings, "The list when you reach a shop", "Make link". The link is shown once,
  because only its hash is stored. "Replace link" makes a new one and stops the old one working,
  so the automations on that phone then need the new link.

The link looks like `https://home.example.com/presence/<43 characters>`. It is personal: whoever
holds it can tell the assistant that you arrived somewhere. Keep it out of screenshots.

### Phone setup, once per place

**Not tried on a phone yet.** The wording in the Shortcuts app differs between iOS versions. The
two variants below are from Apple's Shortcuts User Guide as fetched on 6 Oct 2026 (its iOS 27 and
iOS 18 pages, read through a tool that summarises); "Run Immediately" is the spec's wording and
was not on either page as fetched.

1. Open **Shortcuts** and start a new automation with the trigger **Arrive**.
   - Apple's iOS 27 guide: create a shortcut, tap **Edit**, then **Automation**, and choose **Arrive**.
   - Apple's iOS 18 guide: **Automation**, **+**, **Create Personal Automation**, **Arrive**.
2. Choose the location (search for the shop, or drop a pin on it).
3. Make it run without asking. The iOS 27 guide: in the shortcut's **Info**, **Privacy**, turn on
   **Allow Running When Locked**. Earlier versions: choose **Run Immediately** where the trigger's
   options are set.
4. Add one action: **Get Contents of URL**.
   - URL: your personal link.
   - Tap **Show More**. **Method**: `POST`. **Request Body**: `JSON`.
   - Add two **Text** fields: `event` with the value `enter`, and `place` with the name of the
     place.
5. Save it. To test, run the shortcut by hand while something is on the list for that shop.

The place name must be the name the assistant knows the shop by: the names you gave when it asked
where you shop, listed under Places on the Settings page. Case and extra spaces do not matter.

For **home**, make two automations, with the place `Home`:

| Trigger | `event` | `place` |
| --- | --- | --- |
| Arrive (at home) | `enter` | `Home` |
| Leave (home) | `exit` | `Home` |

The same call from a terminal, for testing:

```sh
curl -i -X POST https://home.example.com/presence/<token> \
     -H 'Content-Type: application/json' -d '{"event": "enter", "place": "Tesco Extra"}'
```

The answer is always `204 No Content`, whether or not the token or the body was any good, so the
response cannot be used to find out whether a link exists. To see what happened, look at the
worker and api logs (`presence`, `presence_ignored`, `presence_rate_limited`).

### What a ping does

`POST /presence/{token}` with `{"event": "enter" | "exit", "place": "<name>"}`. A valid ping is
stored in `presence_events` and one rule runs (`app/presence/rules.py`, no model involved):

| Rule | When | Condition | What happens |
| --- | --- | --- | --- |
| Store arrival | `enter` a place of kind `store` | Something is on the list for this shop, not counting guesses; no list was sent to this person for this shop in the last 2 hours | A DM with the list for this shop, then any "(probably)" entries. Sent at once, even in quiet hours |
| Out and about | `exit` a place of kind `home` | The list has 8 or more entries, or one was added in the last 2 hours; once per person per day | A DM: "You're out. The list has 9 items, want it?" Answering "yes" gets the list |
| Both home | `enter` a place of kind `home` | No adult's phone last said they left home, and a shop list was sent today | Nothing is sent. Today's "out and about" is marked used for every adult |

The list "for this shop" is every entry that names no shop, plus those whose shop reads like this
one: "Tesco" on an entry matches the place "Tesco Extra", "African shop" matches "African shop on
Rye Lane", and "Costco" matches neither.

A place name the assistant has not heard before is added with kind `other` (or `home`, if it is
called Home) and does nothing until someone gives it a kind on the Settings page. A token is good
for 30 pings an hour.

Decisions and their reasons: [ADR 0023](adr/0023-presence-endpoint-and-tokens.md),
[ADR 0024](adr/0024-presence-rules-and-nudge-dedupe.md).

## Predictions

### The consumption model

For each item the assistant learns how long it usually lasts, from that item's own history in
`inventory_events` (`app/services/consumption.py`):

- A **cycle** runs from the latest `restocked` or `added` to the next `finished`, `low` or
  `discarded`. Cycles shorter than 12 hours or longer than 120 days are ignored.
- The **average** is an exponentially weighted mean of cycle lengths, the newest cycle counting
  for half: cycles of 6, 4 and 6 days give 5.5 days.
- With **two or more** cycles, and the item bought since it last ran out, the predicted run-out is
  the last purchase plus the average. Otherwise there is no prediction.

Undone events are not learned from. The result is one row per item in `consumption_profiles`.

### "Probably" entries on the list

An item predicted to run out within two days goes on the shopping list as a guess (`reason =
'predicted'`). Guesses are shown apart from what was asked for: "(probably)" in chat, "Probably
needed" on the Shopping page, a trailing section in the list sent at a shop.

| What happens | The guess |
| --- | --- |
| Someone asks for the item, or says it is low or finished | Becomes a real entry |
| The item is bought | Is ticked off with it |
| Someone takes it off the list | Stays off until the item has been bought again |
| A week passes after the predicted day with no word | Is dropped: the guess was wrong |
| "Got everything on the list" | Is left alone; only what was asked for is marked bought |

### The jobs

| Job | When, household time | What it does |
| --- | --- | --- |
| `consumption_model` | Once a day, from 03:00. If the worker was down then, as soon as it is back | Recomputes every profile and the guesses. Then asks the model once per household to put items with no category into a supermarket section (`LLM_FAST_MODEL`), so lists are grouped by aisle |
| `low_stock_prompt` | 17:30, or up to four hours late | Recomputes, then sends the household "Probably running low: milk, bread. Add to the list?" for guesses nobody was asked about in the last three days. Nothing to ask, nothing sent |
| `weekly_digest` | Sunday 18:00 | Now also names what will probably run low in the week ahead |

A "yes", "just the milk", or a tick reaction to the prompt is an ordinary message: the agent sees
the question in the thread and updates the list. Reasons: [ADR 0026](adr/0026-consumption-model-and-guessed-list-entries.md).

## Limits

- The phone steps have not been tried on a phone, and Apple renames these screens between iOS
  versions.
- iOS decides when an Arrive or Leave automation fires. It can be a minute late, fire twice, or
  not fire with Location Services or Background App Refresh off. A double ping sends one list.
- The model learns from what it is told. If nobody says the milk ran out, it has no cycle to
  learn from. Receipts that log "semi skimmed milk" beside "milk" split one item's history in two;
  merge the duplicates on the item's page.
- A prediction is per item, not per pack size: a bigger pack looks like a slower household.
